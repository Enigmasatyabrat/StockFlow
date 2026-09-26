"""A vision model served by Ollama: free per image, local or on your own server.

Implements the same :class:`~stockflow.analyzer.Analyzer` protocol as the
Gemini client, sends the same prompt, and constrains the reply with the same
schema (rewritten as JSON Schema), so everything downstream -- validation,
routing, CSVs, embedded metadata -- is unchanged.

Standard library only. Ollama's HTTP API is small, and a dependency on its
Python client would buy nothing but another package to install.

Ollama runs wherever you like. The default host is this machine; for a model
on another computer, point ``--ollama-host`` at it, ideally through an SSH
tunnel (``ssh -L 11434:localhost:11434 user@server``) rather than exposing
Ollama's unauthenticated port on the network.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from .analyzer import parse_analysis
from .errors import AnalyzerError, BackendUnavailable, MalformedResponseError
from .models import Analysis
from .prompt import SYSTEM_PROMPT, build_prompt, json_schema

log = logging.getLogger(__name__)

#: (url, payload or None for GET, timeout seconds) -> decoded JSON body.
Transport = Callable[[str, "dict | None", float], dict]


class OllamaUnavailable(BackendUnavailable):
    """The server can't be reached or doesn't have the model. Retrying won't help."""


def _urllib_transport(url: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Ollama puts the reason in a JSON body, e.g. {"error": "model ... not found"}.
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        raise _HttpError(exc.code, detail or str(exc)) from exc


class _HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class OllamaAnalyzer:
    """Ollama-backed analyzer. One request per image, retried on bad output."""

    def __init__(
        self,
        host: str,
        model: str,
        *,
        max_retries: int = 3,
        timeout: float = 600.0,
        temperature: float = 0.4,
        stop: threading.Event | None = None,
        transport: Transport | None = None,
    ):
        self._host = host.rstrip("/")
        self._model = model
        self._max_retries = max(1, max_retries)
        # Generous: a CPU-only server can take minutes on one image.
        self._timeout = timeout
        self._temperature = temperature
        self._stop = stop or threading.Event()
        self._transport = transport or _urllib_transport
        self._schema = json_schema()
        self.stats: dict[str, int] = {"calls": 0, "retries": 0, "failures": 0,
                                      "prompt_tokens": 0, "output_tokens": 0}
        self._stats_lock = threading.Lock()

    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    # ------------------------------------------------------------ preflight --

    def check(self) -> str | None:
        """None if the server is up and has the model, else a message saying what to do."""
        try:
            body = self._transport(f"{self._host}/api/tags", None, 10.0)
        except Exception as exc:
            return (
                f"Cannot reach Ollama at {self._host} ({exc}).\n"
                "Start it with `ollama serve`, or check --ollama-host / the SSH tunnel."
            )
        names = {m.get("name", "") for m in body.get("models", []) if isinstance(m, dict)}
        # `ollama pull qwen2.5vl` stores the model as "qwen2.5vl:latest".
        wanted = {self._model, f"{self._model}:latest"}
        if not names & wanted:
            return (
                f"Ollama at {self._host} does not have the model {self._model!r}.\n"
                f"Download it with:  ollama pull {self._model}"
            )
        return None

    # -------------------------------------------------------------- analyze --

    def analyze(self, image_bytes: bytes, quality_note: str = "") -> Analysis:
        payload = {
            "model": self._model,
            "stream": False,
            "format": self._schema,
            "options": {"temperature": self._temperature},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_prompt(quality_note),
                    "images": [base64.b64encode(image_bytes).decode("ascii")],
                },
            ],
        }

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            if self._stop.is_set():
                raise AnalyzerError("Cancelled")
            try:
                self._bump("calls")
                body = self._transport(f"{self._host}/api/chat", payload, self._timeout)
                self._bump("prompt_tokens", int(body.get("prompt_eval_count", 0) or 0))
                self._bump("output_tokens", int(body.get("eval_count", 0) or 0))
                content = (body.get("message") or {}).get("content", "")
                if not content:
                    raise MalformedResponseError("Model returned an empty response")
                return parse_analysis(content)

            except _HttpError as exc:
                if exc.code == 404:
                    self._bump("failures")
                    raise OllamaUnavailable(
                        f"{exc} -- download it with: ollama pull {self._model}"
                    ) from exc
                last_error = AnalyzerError(f"Ollama returned HTTP {exc.code}: {exc}")
            except urllib.error.URLError as exc:
                self._bump("failures")
                raise OllamaUnavailable(f"Cannot reach Ollama at {self._host}: {exc.reason}") from exc
            except MalformedResponseError as exc:
                # Small local models occasionally produce unusable JSON even
                # under a schema; a fresh sample usually fixes it.
                last_error = exc
            except (TimeoutError, OSError) as exc:
                last_error = AnalyzerError(f"Ollama request failed: {exc}")

            if attempt < self._max_retries:
                self._bump("retries")
                log.warning("Retry %d/%d (%s)", attempt, self._max_retries, str(last_error)[:120])
                if self._stop.wait(2.0 * attempt):
                    raise AnalyzerError("Cancelled during backoff")

        self._bump("failures")
        raise last_error if isinstance(last_error, AnalyzerError) else AnalyzerError(str(last_error))


def describe(host: str) -> str:
    """Banner text: say plainly where the pixels are going."""
    local = any(h in host for h in ("://localhost", "://127.0.0.1", "://[::1]"))
    return f"{host} ({'this machine' if local else 'remote server'})"


__all__ = ["OllamaAnalyzer", "OllamaUnavailable", "describe"]
