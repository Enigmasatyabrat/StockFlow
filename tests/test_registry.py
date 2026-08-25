"""Registry: migration, crash recovery, and cross-run dedupe."""

from __future__ import annotations

import json

import pytest

from stockflow.errors import RegistryError
from stockflow.hashing import DedupeIndex
from stockflow.models import Status
from stockflow.registry import REGISTRY_NAME, SCHEMA_VERSION, Registry


class TestLoading:
    def test_missing_file_gives_empty_registry(self, tmp_path):
        reg = Registry.load(tmp_path)
        assert reg.items == {}

    def test_corrupt_registry_refuses_rather_than_silently_resetting(self, tmp_path):
        """v4 swallowed the error and returned {}, which makes a fully
        processed folder look untouched -- reprocessing everything and burning
        a whole day of API quota in silence."""
        (tmp_path / REGISTRY_NAME).write_text("{not valid json", encoding="utf-8")
        with pytest.raises(RegistryError, match="unreadable"):
            Registry.load(tmp_path)

    def test_corrupt_registry_can_be_forced(self, tmp_path):
        path = tmp_path / REGISTRY_NAME
        path.write_text("{broken", encoding="utf-8")
        reg = Registry.load(tmp_path, force_fresh=True)
        assert reg.items == {}
        assert list(tmp_path.glob(f"{REGISTRY_NAME}.corrupt-*")), "original must be kept"

    def test_newer_schema_is_refused(self, tmp_path):
        (tmp_path / REGISTRY_NAME).write_text(
            json.dumps({"schema_version": 99, "items": {}}), encoding="utf-8"
        )
        with pytest.raises(RegistryError, match="newer StockFlow"):
            Registry.load(tmp_path)


class TestMigration:
    def test_v0_bare_string_form(self, tmp_path):
        (tmp_path / REGISTRY_NAME).write_text(
            json.dumps({"a.jpg": "READY", "b.jpg": "error"}), encoding="utf-8"
        )
        reg = Registry.load(tmp_path)
        assert reg.status_of("a.jpg") is Status.READY
        assert reg.status_of("b.jpg") is Status.ERROR

    def test_legacy_lowercase_status_is_normalised(self, tmp_path):
        """A legacy lowercase 'ready' never matched the 'READY' constant in v4,
        so those files were silently reprocessed at full API cost."""
        (tmp_path / REGISTRY_NAME).write_text(
            json.dumps({"a.jpg": "ready"}), encoding="utf-8"
        )
        reg = Registry.load(tmp_path)
        assert reg.status_of("a.jpg") is Status.READY
        assert not reg.is_pending("a.jpg", max_attempts=3)

    def test_v0_dict_form_hash_renamed_and_indexed(self, tmp_path):
        (tmp_path / REGISTRY_NAME).write_text(
            json.dumps({"a.jpg": {"status": "READY", "hash": "abc123"}}), encoding="utf-8"
        )
        reg = Registry.load(tmp_path)
        assert reg.items["a.jpg"]["sha256"] == "abc123"
        assert reg.index["sha256"]["abc123"] == "a.jpg"

    def test_migration_writes_a_backup(self, tmp_path):
        (tmp_path / REGISTRY_NAME).write_text(json.dumps({"a.jpg": "READY"}), encoding="utf-8")
        Registry.load(tmp_path)
        assert (tmp_path / f"{REGISTRY_NAME}.v0.bak").exists()

    def test_unknown_fields_survive_migration(self, tmp_path):
        (tmp_path / REGISTRY_NAME).write_text(
            json.dumps({"a.jpg": {"status": "READY", "custom_field": "keep me"}}),
            encoding="utf-8",
        )
        reg = Registry.load(tmp_path)
        assert reg.items["a.jpg"]["custom_field"] == "keep me"

    def test_migrating_current_schema_is_a_noop(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.commit("a.jpg", Status.READY, sha256="x")
        again = Registry.load(tmp_path)
        assert again._data["schema_version"] == SCHEMA_VERSION
        assert again.status_of("a.jpg") is Status.READY


class TestPending:
    def test_unknown_file_is_pending(self, tmp_path):
        assert Registry.load(tmp_path).is_pending("new.jpg", max_attempts=3)

    @pytest.mark.parametrize(
        "status", [Status.READY, Status.DUPLICATE, Status.LOW_RESOLUTION,
                   Status.REVIEW, Status.ERROR_PERMANENT]
    )
    def test_terminal_statuses_are_not_pending(self, tmp_path, status):
        reg = Registry.load(tmp_path)
        reg.commit("a.jpg", status)
        assert not reg.is_pending("a.jpg", max_attempts=3)

    def test_error_retries_until_max_attempts(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.commit("a.jpg", Status.ERROR, attempts=1)
        assert reg.is_pending("a.jpg", max_attempts=3)
        reg.commit("a.jpg", Status.ERROR, attempts=3)
        assert not reg.is_pending("a.jpg", max_attempts=3)

    def test_retry_failed_overrides(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.commit("a.jpg", Status.ERROR_PERMANENT, attempts=9)
        assert reg.is_pending("a.jpg", max_attempts=3, retry_failed=True)


class TestTwoPhaseCommit:
    def test_intent_is_persisted_before_the_move(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.begin("a.jpg", action="move", src="/x/a.jpg", dest="/y/b.jpg")
        reloaded = Registry.load(tmp_path)
        assert reloaded.items["a.jpg"]["intent"]["dest"] == "/y/b.jpg"

    def test_commit_clears_the_intent(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.begin("a.jpg", action="move", src="/x/a.jpg", dest="/y/b.jpg")
        reg.commit("a.jpg", Status.READY, final_name="b.jpg")
        assert "intent" not in Registry.load(tmp_path).items["a.jpg"]

    def test_reconcile_finishes_a_completed_move(self, tmp_path):
        """Crash between the move and the commit: the file is already at the
        destination, so the record is completed rather than reprocessed."""
        src, dest = tmp_path / "a.jpg", tmp_path / "out" / "b.jpg"
        dest.parent.mkdir()
        dest.write_bytes(b"x")
        reg = Registry.load(tmp_path)
        reg.begin("a.jpg", action="move", src=str(src), dest=str(dest))

        notes = reg.reconcile(tmp_path)
        assert notes and "move had completed" in notes[0]
        assert reg.items["a.jpg"]["final_name"] == "b.jpg"
        assert "intent" not in reg.items["a.jpg"]

    def test_reconcile_leaves_an_unstarted_move_pending(self, tmp_path):
        src = tmp_path / "a.jpg"
        src.write_bytes(b"x")
        reg = Registry.load(tmp_path)
        reg.begin("a.jpg", action="move", src=str(src), dest=str(tmp_path / "out" / "b.jpg"))

        notes = reg.reconcile(tmp_path)
        assert "still pending" in notes[0]
        assert reg.is_pending("a.jpg", max_attempts=3)

    def test_reconcile_reports_both_copies(self, tmp_path):
        src, dest = tmp_path / "a.jpg", tmp_path / "out" / "b.jpg"
        dest.parent.mkdir()
        src.write_bytes(b"x")
        dest.write_bytes(b"x")
        reg = Registry.load(tmp_path)
        reg.begin("a.jpg", action="move", src=str(src), dest=str(dest))

        notes = reg.reconcile(tmp_path)
        assert "BOTH" in notes[0]
        assert src.exists() and dest.exists(), "reconcile must never delete either copy"

    def test_reconcile_flags_a_vanished_file(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.begin("a.jpg", action="move", src=str(tmp_path / "gone.jpg"),
                  dest=str(tmp_path / "also-gone.jpg"))
        notes = reg.reconcile(tmp_path)
        assert "missing" in notes[0]
        assert reg.status_of("a.jpg") is Status.ERROR


class TestCrossRunDedupe:
    def test_index_survives_a_reload(self, tmp_path):
        """v4 rebuilt the hash sets empty every run, so a byte-identical file
        submitted in a later batch was never caught."""
        reg = Registry.load(tmp_path)
        reg.commit("a.jpg", Status.READY, sha256="deadbeef", phash="ff00ff00")

        reloaded = Registry.load(tmp_path)
        index = DedupeIndex.from_registry(reloaded)
        assert index.exact_match("deadbeef") == "a.jpg"

    def test_near_match_uses_threshold(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.commit("a.jpg", Status.READY, sha256="s1", phash="8000000000000000")
        index = DedupeIndex.from_registry(Registry.load(tmp_path), threshold=6)
        assert index.near_match("8000000000000000")[0] == "a.jpg"
        assert index.near_match(None) is None


class TestPersistence:
    def test_save_is_atomic_and_leaves_no_temp_files(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.commit("a.jpg", Status.READY)
        assert not list(tmp_path.glob("*.tmp"))

    def test_unicode_round_trips(self, tmp_path):
        reg = Registry.load(tmp_path)
        reg.commit("日本の桜.jpg", Status.READY, title="Café — 日本")
        assert Registry.load(tmp_path).items["日本の桜.jpg"]["title"] == "Café — 日本"
