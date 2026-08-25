"""Throttling primitives and model-limit resolution."""

from __future__ import annotations

import threading
import time

import pytest

from stockflow import limits as limits_mod
from stockflow.hashing import DedupeIndex
from stockflow.ratelimit import AdaptiveGate, DailyQuota, TokenBucket


class TestTokenBucket:
    def test_burst_is_granted_immediately(self):
        bucket = TokenBucket(60, burst=2)
        started = time.monotonic()
        assert bucket.acquire(timeout=1)
        assert time.monotonic() - started < 0.2

    def test_rate_is_enforced_after_the_burst(self):
        bucket = TokenBucket(60, burst=1)  # one per second
        assert bucket.acquire(timeout=1)
        started = time.monotonic()
        assert bucket.acquire(timeout=3)
        assert time.monotonic() - started > 0.5

    def test_stop_event_interrupts_the_wait(self):
        bucket = TokenBucket(1, burst=1)
        bucket.acquire(timeout=1)
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        assert bucket.acquire(stop=stop) is False
        assert time.monotonic() - started < 2, "Ctrl-C must not wait a full interval"

    def test_timeout_returns_false(self):
        bucket = TokenBucket(1, burst=1)
        bucket.acquire(timeout=1)
        assert bucket.acquire(timeout=0.1) is False

    def test_rate_can_be_updated_at_runtime(self):
        """A 429 tells us the real limit; the limiter has to be able to adopt it."""
        bucket = TokenBucket(60)
        bucket.update_rate(6)
        assert bucket.rate == pytest.approx(0.1)


class TestAdaptiveGate:
    def test_open_by_default(self):
        started = time.monotonic()
        AdaptiveGate().wait()
        assert time.monotonic() - started < 0.1

    def test_trip_pauses_every_caller(self):
        """Workers must slow together; each backing off privately means the
        pool keeps hammering an API that is already refusing requests."""
        gate = AdaptiveGate()
        gate.trip(0.4)
        started = time.monotonic()
        gate.wait()
        assert time.monotonic() - started >= 0.3

    def test_longest_pause_wins(self):
        gate = AdaptiveGate()
        gate.trip(5.0)
        gate.trip(0.1)
        assert gate.paused_for > 4

    def test_clear_releases(self):
        gate = AdaptiveGate()
        gate.trip(10)
        gate.clear()
        assert gate.paused_for == 0

    def test_counts_trips(self):
        gate = AdaptiveGate()
        gate.trip(0.01)
        gate.trip(0.01)
        assert gate.trips == 2


class TestDailyQuota:
    def test_consumes_up_to_the_limit(self, tmp_path):
        quota = DailyQuota(tmp_path / "q.json", "model-x", limit=3)
        assert [quota.try_consume() for _ in range(4)] == [True, True, True, False]
        assert quota.remaining == 0

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "q.json"
        first = DailyQuota(path, "model-x", limit=10)
        first.try_consume()
        first.try_consume()
        assert DailyQuota(path, "model-x", limit=10).used == 2

    def test_counter_is_per_model(self, tmp_path):
        path = tmp_path / "q.json"
        DailyQuota(path, "model-a", limit=10).try_consume()
        assert DailyQuota(path, "model-b", limit=10).used == 0

    def test_limit_can_be_corrected(self, tmp_path):
        quota = DailyQuota(tmp_path / "q.json", "m", limit=1000)
        quota.set_limit(50)
        assert quota.limit == 50

    def test_unreadable_file_does_not_crash(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text("{broken", encoding="utf-8")
        assert DailyQuota(path, "m", limit=5).used == 0


class TestModelLimits:
    def test_known_model(self):
        assert limits_mod.for_model("gemini-2.5-flash-lite").rpm == 15

    def test_point_release_matches_base_model(self):
        base = limits_mod.for_model("gemini-2.5-flash-lite")
        variant = limits_mod.for_model("gemini-2.5-flash-lite-preview-09-2025")
        assert variant.rpm == base.rpm

    def test_unknown_family_is_inferred(self):
        assert limits_mod.for_model("gemini-9.9-flash-lite-future").rpm == 15

    def test_completely_unknown_is_conservative(self):
        """Better to under-drive an unknown model than to trip a 429 storm."""
        unknown = limits_mod.for_model("some-other-vendor-model")
        assert unknown.rpm <= 5

    def test_source_is_recorded(self):
        assert limits_mod.for_model("gemini-2.5-flash-lite").source

    @pytest.mark.parametrize(
        "quota_id,field,value",
        [
            ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "rpd", 250),
            ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "rpm", 8),
            ("GenerateContentInputTokensPerMinutePerProjectPerModel", "tpm", 1000),
        ],
    )
    def test_observed_quota_is_adopted(self, quota_id, field, value):
        base = limits_mod.for_model("gemini-2.5-flash-lite")
        updated = limits_mod.observe_quota_value(base, quota_id, value)
        assert getattr(updated, field) == value
        assert "observed" in updated.source

    def test_nonsense_quota_value_ignored(self):
        base = limits_mod.for_model("gemini-2.5-flash-lite")
        assert limits_mod.observe_quota_value(base, "whatever", 0) is base


class TestDedupeIndex:
    def test_exact_match(self):
        index = DedupeIndex()
        index.add("a.jpg", "sha-1", None)
        assert index.exact_match("sha-1") == "a.jpg"
        assert index.exact_match("sha-2") is None

    def test_first_filename_wins(self):
        index = DedupeIndex()
        index.add("a.jpg", "sha-1", None)
        index.add("b.jpg", "sha-1", None)
        assert index.exact_match("sha-1") == "a.jpg"

    def test_near_match_respects_threshold(self):
        index = DedupeIndex(threshold=0)
        index.add("a.jpg", "s", "8000000000000000")
        assert index.near_match("8000000000000000")[0] == "a.jpg"
        assert index.near_match("ffffffffffffffff") is None

    def test_none_phash_is_safe(self):
        assert DedupeIndex().near_match(None) is None
