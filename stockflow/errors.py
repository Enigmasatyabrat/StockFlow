"""Exception hierarchy for StockFlow.

Tier 0: this module must not import anything else from the package.
"""

from __future__ import annotations


class StockFlowError(Exception):
    """Base class for every error StockFlow raises deliberately."""


class ConfigError(StockFlowError):
    """Settings could not be resolved (bad flag, missing key, unreadable config)."""


class UnsupportedFormatError(StockFlowError):
    """The file extension is known but this build can't decode it.

    Most commonly a RAW file when the optional `rawpy` extra isn't installed.
    Raised instead of a bare ImportError traceback so the CLI can print
    something a photographer can act on.
    """


class ImageDecodeError(StockFlowError):
    """The file claims to be an image but the pixels could not be read."""


class AnalyzerError(StockFlowError):
    """The vision model call failed."""


class DailyQuotaExhausted(AnalyzerError):
    """The model's whole-day quota is gone.

    Not the photo's fault, so the run stops cleanly instead of retrying or
    blacklisting anything. Distinguished from `RateLimited` by the `quotaId`
    in the 429 body, never by the suggested retry delay -- Google returns a
    short retryDelay (e.g. "34s") even for per-day exhaustion, and sleeping on
    it just burns the rest of the day re-hitting the same wall.
    """


class RateLimited(AnalyzerError):
    """A short per-minute rate-limit burst. Worth waiting out and retrying."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class MalformedResponseError(AnalyzerError):
    """The model returned something that isn't usable analysis."""


class MetadataWriteError(StockFlowError):
    """exiftool refused to write, or wrote something we can't trust."""


class RegistryError(StockFlowError):
    """The per-folder state file is unreadable, corrupt, or from a newer version."""
