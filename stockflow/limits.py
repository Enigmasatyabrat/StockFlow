"""Per-model rate limits.

IMPORTANT: Google no longer publishes a per-model RPM/TPM/RPD table on
ai.google.dev -- the public rate-limits page documents only the *mechanics*
and points you at an authenticated AI Studio page for the actual numbers.
That means there is no source of truth this file can be pinned to.

So every number here is a DEFAULT, not a fact. Two consequences drive the
design:

1. The user can override any of it (``--rpm``, ``--rpd``, config file).
2. The tool self-corrects at runtime: a 429 carries a ``QuotaFailure``
   violation with the real ``quotaValue``, and :func:`observe_quota_value`
   folds that back in so later runs use the true limit.

Confidence on the numbers below is "third-party aggregators agree", not
"Google says so". They are deliberately conservative.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelLimits:
    """Requests-per-minute / tokens-per-minute / requests-per-day for one model."""

    rpm: int
    tpm: int
    rpd: int
    #: Where these numbers came from, surfaced in the startup banner so nobody
    #: mistakes them for gospel.
    source: str = "default estimate (unverified)"


# Free-tier defaults. Unverified -- see module docstring.
_FREE_TIER: dict[str, ModelLimits] = {
    "gemini-2.5-flash-lite": ModelLimits(15, 250_000, 1_000, "third-party estimate"),
    "gemini-2.5-flash": ModelLimits(10, 250_000, 250, "third-party estimate"),
    "gemini-2.5-pro": ModelLimits(5, 250_000, 100, "third-party estimate"),
    "gemini-3.5-flash-lite": ModelLimits(15, 250_000, 1_000, "third-party estimate"),
    "gemini-3.5-flash": ModelLimits(10, 250_000, 250, "third-party estimate"),
}

#: Used when the model id isn't recognised at all. Slow but safe: better to
#: under-drive an unknown model than to hammer it into a 429 storm.
FALLBACK = ModelLimits(5, 100_000, 100, "unknown model - conservative fallback")


def for_model(model: str) -> ModelLimits:
    """Best-known limits for ``model``, falling back on family prefix then FALLBACK."""
    if model in _FREE_TIER:
        return _FREE_TIER[model]

    # Unknown point release (e.g. "gemini-2.5-flash-lite-preview-09-2025"):
    # fall back to the longest known id that prefixes it.
    matches = [k for k in _FREE_TIER if model.startswith(k)]
    if matches:
        return _FREE_TIER[max(matches, key=len)]

    # Unknown but recognisable family.
    if "flash-lite" in model:
        return replace(_FREE_TIER["gemini-2.5-flash-lite"], source="inferred from model name")
    if "flash" in model:
        return replace(_FREE_TIER["gemini-2.5-flash"], source="inferred from model name")
    if "pro" in model:
        return replace(_FREE_TIER["gemini-2.5-pro"], source="inferred from model name")
    return FALLBACK


def observe_quota_value(limits: ModelLimits, quota_id: str, value: int) -> ModelLimits:
    """Fold a real ``quotaValue`` observed in a 429 back into our limits.

    Google's QuotaFailure violations name the dimension in ``quotaId``, e.g.
    ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier``. Trusting that
    number beats trusting anything hardcoded here.
    """
    if value <= 0:
        return limits
    qid = quota_id.lower()
    src = f"observed from API 429 ({quota_id}={value})"
    if "perday" in qid:
        return replace(limits, rpd=value, source=src)
    if "perminute" in qid and "token" in qid:
        return replace(limits, tpm=value, source=src)
    if "perminute" in qid:
        return replace(limits, rpm=value, source=src)
    return limits
