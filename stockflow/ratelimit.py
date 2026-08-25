"""Shared throttling: a token bucket, an adaptive gate, and a daily counter.

v4 slept a flat 4 seconds after every image, which is both too slow when the
API is healthy and too fast when it is not. These primitives replace it with
something that tracks the real limit and reacts to real 429s.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _pacific_tz():
    """Google resets requests-per-day quotas at midnight US Pacific.

    A real timezone is preferred because a fixed offset is wrong for half the
    year, but Windows ships no system tz database and ``tzdata`` may be
    missing. Falling back to PST rather than raising keeps the tool usable --
    the only cost is that the daily rollover is an hour off during daylight
    saving, which merely shifts when the counter resets.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/Los_Angeles")
    except Exception:
        log.debug("tzdata unavailable; approximating US Pacific as UTC-8")
        return timezone(timedelta(hours=-8), "PST")


PACIFIC = _pacific_tz()

QUOTA_FILE = ".stockflow_quota.json"


class TokenBucket:
    """Monotonic token bucket shared by every worker.

    Uses ``time.monotonic`` rather than ``time.time`` so an NTP correction or
    a DST change can't make the limiter hand out a burst of free permits.
    """

    def __init__(self, rate_per_minute: int, burst: int = 2):
        self.rate = max(1, rate_per_minute) / 60.0
        # A small burst on purpose: Google enforces a sliding window, so
        # emptying a full-size bucket at t=0 trips a 429 immediately.
        self.capacity = max(1.0, float(min(burst, max(1, rate_per_minute))))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._cond = threading.Condition()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self, stop: threading.Event | None = None, timeout: float | None = None) -> bool:
        """Block until a permit is available. False if interrupted or timed out."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                if stop is not None and stop.is_set():
                    return False
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                needed = (1.0 - self._tokens) / self.rate
                # Cap the wait so a Ctrl-C is noticed promptly rather than
                # after a full inter-token interval.
                wait = min(needed, 0.5)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait = min(wait, remaining)
                self._cond.wait(timeout=wait)

    def update_rate(self, rate_per_minute: int) -> None:
        with self._cond:
            self.rate = max(1, rate_per_minute) / 60.0
            self.capacity = max(1.0, min(self.capacity, float(rate_per_minute)))
            self._cond.notify_all()


class AdaptiveGate:
    """A shared pause that slows every worker at once.

    Without this, N workers each discover the same project-wide 429
    independently and each backs off privately, so the pool keeps hammering
    the API while every individual worker believes it is being polite.
    """

    def __init__(self) -> None:
        self._pause_until = 0.0
        self._cond = threading.Condition()
        self.trips = 0

    def wait(self, stop: threading.Event | None = None) -> None:
        with self._cond:
            while True:
                if stop is not None and stop.is_set():
                    return
                remaining = self._pause_until - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(timeout=min(remaining, 0.5))

    def trip(self, seconds: float) -> None:
        """Pause the whole pool for at least ``seconds``."""
        with self._cond:
            self.trips += 1
            target = time.monotonic() + max(0.0, seconds)
            self._pause_until = max(self._pause_until, target)
            self._cond.notify_all()

    def clear(self) -> None:
        with self._cond:
            self._pause_until = 0.0
            self._cond.notify_all()

    @property
    def paused_for(self) -> float:
        return max(0.0, self._pause_until - time.monotonic())


class DailyQuota:
    """Persistent requests-per-day counter, rolled over on Pacific midnight.

    Enforced as a pre-flight gate rather than discovered by exception, so a
    run that cannot finish says so before spending anything.
    """

    def __init__(self, path: Path, model: str, limit: int):
        self.path = path
        self.model = model
        self.limit = limit
        self._lock = threading.Lock()
        self._used = 0
        self._date = self._today()
        self._observed_tokens: dict[str, int] = {}
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(PACIFIC).strftime("%Y-%m-%d")

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if raw.get("pacific_date") == self._date and raw.get("model") == self.model:
            self._used = int(raw.get("requests", 0))
            self._observed_tokens = dict(raw.get("tokens", {}))

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(
                    {
                        "pacific_date": self._date,
                        "model": self.model,
                        "requests": self._used,
                        "limit": self.limit,
                        "tokens": self._observed_tokens,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.debug("Could not persist quota file: %s", exc)

    def _roll(self) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._used = 0

    @property
    def used(self) -> int:
        with self._lock:
            self._roll()
            return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def try_consume(self) -> bool:
        with self._lock:
            self._roll()
            if self._used >= self.limit:
                return False
            self._used += 1
            self._save()
            return True

    def record_tokens(self, prompt_tokens: int, output_tokens: int) -> None:
        """Remember observed token usage so cost projections stop being guesses."""
        with self._lock:
            if prompt_tokens:
                self._observed_tokens["prompt"] = prompt_tokens
            if output_tokens:
                self._observed_tokens["output"] = output_tokens

    def set_limit(self, limit: int) -> None:
        with self._lock:
            self.limit = limit
            self._save()

    @property
    def observed_tokens(self) -> dict[str, int]:
        return dict(self._observed_tokens)
