"""The vision-model interface: a Protocol, a real Gemini client, and a fake.

The Protocol is what makes the pipeline testable -- the entire test suite runs
against `FakeAnalyzer` with no API key and no network.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from typing import Any, Protocol

from .errors import (
    AnalyzerError,
    DailyQuotaExhausted,
    MalformedResponseError,
    RateLimited,
)
from .models import Analysis
from .prompt import RESPONSE_SCHEMA, SYSTEM_PROMPT, build_prompt
from .ratelimit import AdaptiveGate, TokenBucket
from .rules import as_bool, as_int, as_text, clean_keywords, normalize_category, normalize_risk

log = logging.getLogger(__name__)


class Analyzer(Protocol):
    """Anything that can turn image bytes into an :class:`Analysis`."""

    def analyze(self, image_bytes: bytes, quality_note: str = "") -> Analysis: ...


# ------------------------------------------------------------- parsing --

def parse_analysis(data: Any) -> Analysis:
    """Validate and coerce a raw model response into an Analysis.

    Every field is coerced defensively. Structured output makes the *shape*
    reliable, but Google's own documentation still says to validate values --
    and the coercions here (JSON null for a string, "false" for a boolean,
    "85/100" for an integer, "HIGH" for an enum) are all failure modes v4 hit
    in production.
    """
    if isinstance(data, str):
        try:
            data = json.loads(_strip_fences(data))
        except Exception as exc:
            raise MalformedResponseError(f"Response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedResponseError(f"Expected a JSON object, got {type(data).__name__}")

    title = as_text(data.get("title"))
    if not title:
        raise MalformedResponseError("Model returned no title")

    raw_keywords = data.get("keywords")
    if not isinstance(raw_keywords, (list, tuple)):
        raise MalformedResponseError("Model returned no usable keyword list")
    keywords = clean_keywords(raw_keywords)
    if not keywords:
        raise MalformedResponseError("Model returned no usable keywords")

    category, category2, _ = normalize_category(data.get("category"), data.get("category2"))

    return Analysis(
        title=title,
        description=as_text(data.get("description")),
        keywords=tuple(keywords),
        category=category,
        category2=category2,
        commercial_score=as_int(data.get("commercial_score"), default=0),
        rejection_risk=normalize_risk(data.get("rejection_risk")),
        rejection_reason=as_text(data.get("rejection_reason")),
        people_visible=as_bool(data.get("people_visible"), default=True),
        property_or_trademark_visible=as_bool(data.get("property_or_trademark_visible")),
        watermark_or_overlay_visible=as_bool(data.get("watermark_or_overlay_visible")),
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1).strip() if m else text


# ------------------------------------------------------- error classification --

def classify_api_error(exc: Exception) -> Exception:
    """Map an SDK exception onto our own error types.

    Reads the structured ``details`` the SDK already parsed rather than
    regex-scraping the message. The important subtlety: a per-day 429 comes
    back with a short ``retryDelay`` (values like "34s" have been observed),
    so honouring that delay would just re-hit the same wall for the rest of the
    day. ``quotaId`` is the only trustworthy discriminator.
    """
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "")
    message = str(exc)
    details = getattr(exc, "details", None)

    is_429 = code == 429 or "RESOURCE_EXHAUSTED" in status or "429" in message
    if not is_429:
        return exc

    quota_ids: list[str] = []
    retry_after: float | None = None

    entries: list = []
    if isinstance(details, dict):
        err = details.get("error", details)
        if isinstance(err, dict):
            raw = err.get("details")
            if isinstance(raw, list):
                entries = raw
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        etype = str(entry.get("@type", ""))
        if etype.endswith("QuotaFailure"):
            for violation in entry.get("violations", []) or []:
                if isinstance(violation, dict) and violation.get("quotaId"):
                    quota_ids.append(str(violation["quotaId"]))
        elif etype.endswith("RetryInfo"):
            delay = str(entry.get("retryDelay", ""))
            m = re.match(r"^([\d.]+)s?$", delay)
            if m:
                retry_after = float(m.group(1))

    joined = " ".join(quota_ids).lower()
    if "perday" in joined:
        return DailyQuotaExhausted(message)

    # Fall back to the message text when no structured details arrived -- bare
    # 429 bodies with no `details` array do occur.
    if not quota_ids and re.search(r"per\s*day|perday|daily", message, re.IGNORECASE):
        return DailyQuotaExhausted(message)

    if retry_after is None:
        m = re.search(r"retry in ([\d.]+)\s*s", message, re.IGNORECASE)
        if m:
            retry_after = float(m.group(1))
    return RateLimited(message, retry_after=retry_after)


def extract_quota_values(exc: Exception) -> list[tuple[str, int]]:
    """(quotaId, quotaValue) pairs from a 429, for self-correcting our limits."""
    out: list[tuple[str, int]] = []
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return out
    err = details.get("error", details)
    entries = err.get("details") if isinstance(err, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict) or not str(entry.get("@type", "")).endswith("QuotaFailure"):
            continue
        for violation in entry.get("violations", []) or []:
            if not isinstance(violation, dict):
                continue
            qid, qval = violation.get("quotaId"), violation.get("quotaValue")
            if qid and qval is not None:
                try:
                    out.append((str(qid), int(qval)))
                except (TypeError, ValueError):
                    pass
    return out


# --------------------------------------------------------- the real client --

class GeminiAnalyzer:
    """Gemini-backed analyzer with rate limiting and structured retry."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        bucket: TokenBucket | None = None,
        gate: AdaptiveGate | None = None,
        max_retries: int = 5,
        stop: threading.Event | None = None,
        on_quota_observed=None,
    ):
        from google import genai
        from google.genai import types

        self._types = types
        self._model = model
        self._max_retries = max_retries
        self._bucket = bucket
        self._gate = gate
        self._stop = stop or threading.Event()
        self._on_quota_observed = on_quota_observed
        self.stats: dict[str, int] = {"calls": 0, "retries": 0, "failures": 0,
                                      "prompt_tokens": 0, "output_tokens": 0}
        self._stats_lock = threading.Lock()

        # Let the SDK retry pure transport faults, but NEVER 429 -- quota
        # decisions belong to us, and the SDK's blind backoff would spend a
        # daily-exhausted quota five times over before we ever saw the error.
        http_options = types.HttpOptions(
            retry_options=types.HttpRetryOptions(
                attempts=3,
                initial_delay=2.0,
                max_delay=30.0,
                exp_base=2,
                jitter=1,
                http_status_codes=[408, 500, 502, 503, 504],
            )
        )
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def analyze(self, image_bytes: bytes, quality_note: str = "") -> Analysis:
        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.4,
        )
        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            build_prompt(quality_note),
        ]

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            if self._stop.is_set():
                raise AnalyzerError("Cancelled")
            if self._gate is not None:
                self._gate.wait(self._stop)
            if self._bucket is not None and not self._bucket.acquire(self._stop):
                raise AnalyzerError("Cancelled while waiting for a rate-limit permit")

            try:
                self._bump("calls")
                response = self._client.models.generate_content(
                    model=self._model, contents=contents, config=config
                )
                self._record_usage(response)
                return parse_analysis(self._payload(response))

            except DailyQuotaExhausted:
                raise
            except Exception as exc:
                mapped = classify_api_error(exc)

                if isinstance(mapped, DailyQuotaExhausted):
                    raise mapped from exc

                for quota_id, value in extract_quota_values(exc):
                    if self._on_quota_observed:
                        self._on_quota_observed(quota_id, value)

                last_error = mapped
                if attempt >= self._max_retries:
                    break

                wait = self._backoff(mapped, attempt)
                if isinstance(mapped, RateLimited) and self._gate is not None:
                    # Slow every worker, not just this one.
                    self._gate.trip(wait)
                self._bump("retries")
                log.warning(
                    "Retry %d/%d in %.0fs (%s)",
                    attempt, self._max_retries, wait, str(mapped)[:120],
                )
                if self._stop.wait(wait):
                    raise AnalyzerError("Cancelled during backoff") from exc

        self._bump("failures")
        raise AnalyzerError(str(last_error) if last_error else "Unknown API failure")

    @staticmethod
    def _backoff(exc: Exception, attempt: int) -> float:
        if isinstance(exc, RateLimited) and exc.retry_after:
            return float(exc.retry_after) + random.uniform(0, 2)
        if isinstance(exc, RateLimited):
            return min(15.0 * attempt, 90.0) + random.uniform(0, 3)
        if isinstance(exc, MalformedResponseError):
            return 2.0 * attempt
        return min(5.0 * (2 ** (attempt - 1)), 60.0) + random.uniform(0, 3)

    def _payload(self, response: Any) -> Any:
        """Prefer the SDK's parsed object; fall back to raw text."""
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, dict):
            return parsed
        text = getattr(response, "text", None)
        if not text:
            # Safety blocks and truncation both surface as an empty text field.
            reason = ""
            for cand in getattr(response, "candidates", None) or []:
                if getattr(cand, "finish_reason", None):
                    reason = f" (finish_reason={cand.finish_reason})"
                    break
            raise MalformedResponseError(f"Model returned an empty response{reason}")
        return text

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        self._bump("prompt_tokens", int(getattr(usage, "prompt_token_count", 0) or 0))
        self._bump("output_tokens", int(getattr(usage, "candidates_token_count", 0) or 0))


# --------------------------------------------------------------- the fake --

class FakeAnalyzer:
    """Deterministic in-memory analyzer for tests and ``--dry-run``."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        default: dict | None = None,
        errors: list[Exception | None] | None = None,
        delay: float = 0.0,
    ):
        self.responses = list(responses or [])
        self.errors = list(errors or [])
        self.default = default
        self.delay = delay
        self.calls: list[bytes] = []
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    @staticmethod
    def sample(**overrides: Any) -> dict:
        data = {
            "title": "Sunlit home office desk with laptop and coffee cup",
            "description": "A tidy home office desk lit by morning sun, with an open "
                           "laptop and a ceramic coffee cup. Useful for remote-work articles.",
            "keywords": [f"keyword{i}" for i in range(1, 45)],
            "category": "Business/Finance",
            "category2": "Interiors",
            "commercial_score": 78,
            "rejection_risk": "Low",
            "rejection_reason": "",
            "people_visible": False,
            "property_or_trademark_visible": False,
            "watermark_or_overlay_visible": False,
        }
        data.update(overrides)
        return data

    def analyze(self, image_bytes: bytes, quality_note: str = "") -> Analysis:
        with self._lock:
            index = len(self.calls)
            self.calls.append(image_bytes)
            self.prompts.append(quality_note)
        if self.delay:
            time.sleep(self.delay)

        if index < len(self.errors) and self.errors[index] is not None:
            raise self.errors[index]
        if index < len(self.responses):
            return parse_analysis(self.responses[index])
        return parse_analysis(self.default or self.sample())
