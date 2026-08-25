"""Per-folder processing state, with crash-safe commits.

THE CRASH WINDOW THIS CLOSES
----------------------------
v4's per-image sequence was: move file -> write CSV row -> write registry.
Once the file leaves the scan root it is invisible to the next run's
``iterdir()``. So any failure after the move but before the registry write
stranded the photo permanently: sitting in an output folder, absent from the
CSV, absent from the registry, and never reprocessed. A CSV row failing
because the user had the file open in Excel was enough to trigger it.

The fix is a two-phase commit. An *intent* is recorded and flushed BEFORE the
move; the committed record replaces it after. On startup, ``reconcile()``
inspects any surviving intent and repairs from whatever is actually on disk.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import RegistryError
from .models import Status

log = logging.getLogger(__name__)

REGISTRY_NAME = ".pipeline_registry.json"
SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- migration --

def _migrate_0_to_1(raw: dict) -> dict:
    """Legacy flat ``{filename: status-or-dict}`` -> versioned ``items`` map."""
    items: dict[str, dict] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            status = value.strip().upper()
            # v4 stored an implicit attempt count for the bare-string form.
            items[name] = {"status": status, "attempts": 0 if status == "ERROR" else 1}
        elif isinstance(value, dict):
            entry = dict(value)
            if "status" in entry:
                # Uppercase-normalise: a legacy lowercase "ready" never matched
                # the "READY" constant, so those files were silently reprocessed.
                entry["status"] = str(entry["status"]).strip().upper()
            if "hash" in entry and "sha256" not in entry:
                entry["sha256"] = entry.pop("hash")
            items[name] = entry
    return {"schema_version": 1, "items": items}


def _migrate_1_to_2(raw: dict) -> dict:
    """Add the top-level dedupe index, harvested from per-item hashes."""
    items = raw.get("items", {})
    sha_index: dict[str, str] = {}
    phash_index: dict[str, str] = {}
    for name, entry in items.items():
        sha = entry.get("sha256") or entry.get("hash")
        if sha:
            sha_index.setdefault(sha, name)
        if entry.get("phash"):
            phash_index[name] = entry["phash"]
    raw["schema_version"] = 2
    raw["index"] = {"sha256": sha_index, "phash": phash_index}
    return raw


_MIGRATIONS = {0: _migrate_0_to_1, 1: _migrate_1_to_2}


class Registry:
    """Thread-safe, atomically-persisted state for one working folder."""

    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = data or {
            "schema_version": SCHEMA_VERSION,
            "created_utc": _utc_now(),
            "updated_utc": _utc_now(),
            "items": {},
            "index": {"sha256": {}, "phash": {}},
        }
        self._dirty = False

    # ------------------------------------------------------------ loading --

    @classmethod
    def load(cls, folder: Path, *, force_fresh: bool = False) -> "Registry":
        path = folder / REGISTRY_NAME
        if not path.exists():
            return cls(path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("registry root is not a JSON object")
        except Exception as exc:
            # v4 swallowed this and returned {}, which makes a fully-processed
            # 500-image folder look untouched -- reprocessing everything and
            # burning an entire day's API quota in silence. Loud beats expensive.
            if not force_fresh:
                raise RegistryError(
                    f"{path} is unreadable ({exc}).\n"
                    f"Its contents are the only record of what has already been processed, "
                    f"so StockFlow will not guess.\n"
                    f"Move it aside and re-run with --force-fresh-registry to start over, "
                    f"or restore it from a backup."
                ) from exc
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            broken = path.with_name(f"{REGISTRY_NAME}.corrupt-{stamp}")
            shutil.move(str(path), str(broken))
            log.warning("Corrupt registry moved to %s; starting fresh.", broken.name)
            return cls(path)

        version = int(raw.get("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise RegistryError(
                f"{path} was written by a newer StockFlow (schema v{version}, "
                f"this build understands v{SCHEMA_VERSION}). Upgrade StockFlow."
            )

        if version < SCHEMA_VERSION:
            backup = path.with_name(f"{REGISTRY_NAME}.v{version}.bak")
            if not backup.exists():
                try:
                    shutil.copy2(path, backup)
                    log.info("Backed up v%d registry to %s", version, backup.name)
                except Exception as exc:
                    log.warning("Could not back up registry: %s", exc)
            while version < SCHEMA_VERSION:
                raw = _MIGRATIONS[version](raw)
                version = int(raw["schema_version"])

        raw.setdefault("items", {})
        raw.setdefault("index", {"sha256": {}, "phash": {}})
        raw["index"].setdefault("sha256", {})
        raw["index"].setdefault("phash", {})
        return cls(path, raw)

    # ------------------------------------------------------------- access --

    @property
    def items(self) -> dict[str, dict]:
        return self._data["items"]

    @property
    def index(self) -> dict[str, dict]:
        return self._data["index"]

    def get(self, name: str) -> dict | None:
        with self._lock:
            return self.items.get(name)

    def status_of(self, name: str) -> Status | None:
        entry = self.get(name)
        if not entry:
            return None
        try:
            return Status(str(entry.get("status", "")).upper())
        except ValueError:
            return None

    def attempts_of(self, name: str) -> int:
        entry = self.get(name)
        return int(entry.get("attempts", 0)) if entry else 0

    def is_pending(self, name: str, max_attempts: int, retry_failed: bool = False) -> bool:
        from .rules import should_retry

        entry = self.get(name)
        if entry is None:
            return True
        status = self.status_of(name)
        if retry_failed and status in {Status.ERROR, Status.ERROR_PERMANENT}:
            return True
        return should_retry(status, int(entry.get("attempts", 0)), max_attempts)

    # -------------------------------------------------------- two-phase commit --

    def begin(self, name: str, *, action: str, src: str, dest: str, **extra: Any) -> None:
        """Record what is *about to* happen, and flush before it happens."""
        with self._lock:
            entry = dict(self.items.get(name, {}))
            entry["intent"] = {
                "action": action, "src": src, "dest": dest, "at": _utc_now(), **extra,
            }
            self.items[name] = entry
            self._dirty = True
            self.save()

    def commit(self, name: str, status: Status, **fields: Any) -> None:
        """Record the finished outcome and clear any intent."""
        with self._lock:
            entry = dict(self.items.get(name, {}))
            entry.pop("intent", None)
            entry.update(fields)
            entry["status"] = str(status)
            entry["committed_utc"] = _utc_now()
            self.items[name] = entry
            if fields.get("sha256"):
                self.index["sha256"].setdefault(fields["sha256"], name)
            if fields.get("phash"):
                self.index["phash"][name] = fields["phash"]
            self._dirty = True
            self.save()

    def abandon(self, name: str) -> None:
        """Drop a pending intent without committing (the move never happened)."""
        with self._lock:
            entry = self.items.get(name)
            if entry and "intent" in entry:
                entry.pop("intent", None)
                self._dirty = True
                self.save()

    def reconcile(self, folder: Path) -> list[str]:
        """Repair interrupted commits. Returns human-readable notes."""
        notes: list[str] = []
        with self._lock:
            for name, entry in list(self.items.items()):
                intent = entry.get("intent")
                if not intent:
                    continue
                src = Path(intent.get("src", ""))
                dest = Path(intent.get("dest", ""))
                src_there = src.exists()
                dest_there = dest.exists()

                if dest_there and not src_there:
                    # The move landed; only the commit was lost.
                    entry.pop("intent", None)
                    entry.setdefault("status", str(Status.REVIEW))
                    entry["final_name"] = dest.name
                    entry["destination"] = dest.parent.name
                    entry["reconciled_utc"] = _utc_now()
                    notes.append(
                        f"{name}: move had completed; recorded it as {dest.parent.name}/{dest.name}"
                    )
                elif src_there and not dest_there:
                    # Never happened. Leave the file pending.
                    entry.pop("intent", None)
                    notes.append(f"{name}: interrupted before the move; still pending")
                elif src_there and dest_there:
                    # Derived-file case interrupted between the two moves.
                    entry.pop("intent", None)
                    notes.append(
                        f"{name}: found in BOTH the source folder and {dest.parent.name}; "
                        f"keeping both, please check which you want"
                    )
                else:
                    entry.pop("intent", None)
                    entry["status"] = str(Status.ERROR)
                    entry["last_error"] = "file vanished during an interrupted move"
                    notes.append(f"{name}: file is missing from both locations")
                self._dirty = True
            if notes:
                self.save()
        return notes

    # -------------------------------------------------------------- saving --

    def save(self, force: bool = False) -> None:
        """Atomically persist. Uses a per-process-unique temp name so two
        writers can never interleave into one another's temp file."""
        with self._lock:
            if not self._dirty and not force:
                return
            self._data["updated_utc"] = _utc_now()
            self._data["schema_version"] = SCHEMA_VERSION
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                tmp.write_text(
                    json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                os.replace(tmp, self.path)
                self._dirty = False
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
