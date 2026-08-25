"""The orchestrator: scan, prepare, analyse, classify, commit.

CRASH SAFETY
------------
Every file move is a two-phase commit against the registry: the intent is
written and flushed *before* the move, and replaced by the committed record
after. A crash anywhere in between is repaired on the next run by
``Registry.reconcile``, which inspects what is actually on disk.

This matters because a move is irreversible from the scanner's point of view:
once a file leaves the scan root, ``iter_source_files`` will never see it
again, so a lost registry write means a permanently stranded photo.

CONCURRENCY
-----------
Work is a mix of blocking-but-GIL-releasing operations (PIL decode, numpy,
hashlib, subprocess, socket I/O), so a thread pool is the right tool. API
calls are additionally gated by a shared token bucket and a shared adaptive
pause, so the pool as a whole respects the rate limit rather than each worker
believing it is being polite on its own.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import limits as limits_mod
from .analyzer import Analyzer
from .config import Settings
from .errors import (
    DailyQuotaExhausted,
    ImageDecodeError,
    StockFlowError,
    UnsupportedFormatError,
)
from .hashing import DedupeIndex, phash_hex, sha256_file
from .imaging import loader, normalize as normalize_mod, quality as quality_mod
from .metadata import MetadataWriter
from .models import (
    DESTINATION_FOLDERS,
    FOLDER_READY,
    FOLDER_REPORTS,
    FOLDER_SOURCE_ORIGINALS,
    ItemRecord,
    STATUS_FOLDERS,
    Status,
)
from .ratelimit import AdaptiveGate, DailyQuota, TokenBucket, QUOTA_FILE
from .registry import Registry
from .reporting import (
    ADOBE_HEADER,
    SHUTTERSTOCK_HEADER,
    ReviewLog,
    UploadCsv,
    adobe_row,
    build_summary,
    shutterstock_row,
    write_report,
)
from .rules import choose_status, target_filename

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    records: list[dict] = field(default_factory=list)
    counts: dict[Status, int] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    report_paths: dict[str, Path] = field(default_factory=dict)
    stopped_on_quota: bool = False
    reconcile_notes: list[str] = field(default_factory=list)


def safe_move(src: Path, dest_dir: Path, name: str, max_chars: int = 0) -> Path:
    """Move ``src`` into ``dest_dir`` under a collision-free name.

    The destination is claimed atomically with ``O_EXCL`` before the move, so
    two workers can never both decide that ``title-2.jpg`` is free. That race
    is not theoretical here: filenames come from slugified model titles, and
    near-duplicate images reliably produce near-identical titles.

    ``max_chars`` bounds the whole filename. The de-collision suffix is
    absorbed by trimming the stem rather than appended on top, so a name that
    fits the marketplace limit does not quietly exceed it on the second copy.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 0
    while True:
        if n == 0:
            candidate_name = name
        else:
            tag = f"-{n}"
            trimmed = stem
            if max_chars:
                room = max_chars - len(suffix) - len(tag)
                trimmed = stem[: max(1, room)].rstrip("-")
            candidate_name = f"{trimmed}{tag}{suffix}"
        candidate = dest_dir / candidate_name
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            continue
        except OSError as exc:
            raise StockFlowError(f"Cannot write into {dest_dir}: {exc}") from exc
        os.close(fd)
        try:
            # os.replace is atomic and overwrites our placeholder. It requires
            # the same filesystem, which holds here (work dir and destinations
            # are both under the working folder); shutil.move covers the rest.
            os.replace(src, candidate)
        except OSError:
            candidate.unlink(missing_ok=True)
            shutil.move(str(src), str(candidate))
        return candidate


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        analyzer: Analyzer,
        writer: MetadataWriter,
        *,
        registry: Registry | None = None,
        stop: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
        bucket: TokenBucket | None = None,
        gate: AdaptiveGate | None = None,
    ):
        self.settings = settings
        self.analyzer = analyzer
        self.writer = writer
        self.stop = stop or threading.Event()
        self.progress = progress or (lambda msg: None)

        self.registry = registry or Registry.load(
            settings.folder, force_fresh=settings.force_fresh_registry
        )
        self.dedupe = DedupeIndex.from_registry(self.registry, settings.phash_threshold)

        self.review_log = ReviewLog(settings.reports_dir / "needs_review.txt")
        self.shutterstock_csv = UploadCsv(
            settings.reports_dir / "shutterstock_upload.csv", SHUTTERSTOCK_HEADER
        )
        self.adobe_csv = UploadCsv(
            settings.reports_dir / "adobe_stock_upload.csv", ADOBE_HEADER
        )

        self.counts: dict[Status, int] = {s: 0 for s in Status}
        self._counts_lock = threading.Lock()
        self._dedupe_lock = threading.Lock()
        self.records: list[dict] = []
        self._records_lock = threading.Lock()
        self.stopped_on_quota = False

        model_limits = limits_mod.for_model(settings.model)
        self.model_limits = model_limits
        rpm = settings.rpm or model_limits.rpm
        rpd = settings.rpd or model_limits.rpd
        # Shared with the analyzer so throttling applies pool-wide, not per worker.
        self.bucket = bucket or TokenBucket(rpm)
        self.gate = gate or AdaptiveGate()
        self.quota = DailyQuota(settings.folder / QUOTA_FILE, settings.model, rpd)

    # ------------------------------------------------------------- scanning --

    def pending_files(self) -> list[Path]:
        files = []
        for path in loader.iter_source_files(self.settings.folder, self.settings):
            if self.registry.is_pending(
                path.name, self.settings.max_attempts, self.settings.retry_failed
            ):
                files.append(path)
        return files

    def ensure_dirs(self) -> None:
        if self.settings.dry_run:
            return
        for name in DESTINATION_FOLDERS:
            (self.settings.folder / name).mkdir(parents=True, exist_ok=True)
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- the run --

    def run(self) -> RunResult:
        started = time.time()
        self.ensure_dirs()

        notes = self.registry.reconcile(self.settings.folder)
        for note in notes:
            log.warning("Recovered from an interrupted run -- %s", note)
            self._note(f"RECOVERED  {note}")

        pending = self.pending_files()
        if not pending:
            self.progress("Nothing left to process in this folder.")
            return self._finish(started, 0, notes)

        batch = pending[: self.settings.batch_limit]
        self.progress(
            f"Pending: {len(pending)} image(s). Processing {len(batch)} this run."
        )

        if not self.settings.dry_run:
            available = self.quota.remaining
            if available <= 0:
                self.progress(
                    f"Daily quota for {self.settings.model} is already spent "
                    f"({self.quota.used}/{self.quota.limit}). It resets at midnight "
                    f"US Pacific. Nothing was processed."
                )
                return self._finish(started, len(pending), notes)
            if available < len(batch):
                self.progress(
                    f"Only {available} API call(s) left in today's quota; "
                    f"trimming this run from {len(batch)} to {available}."
                )
                batch = batch[:available]

        workers = max(1, min(self.settings.workers, len(batch)))
        if workers == 1:
            for index, path in enumerate(batch, 1):
                if self.stop.is_set():
                    break
                self._run_one(path, index, len(batch))
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sf") as pool:
                futures = {
                    pool.submit(self._run_one, path, i, len(batch)): path
                    for i, path in enumerate(batch, 1)
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        log.error("Worker crashed on %s: %s", futures[future].name, exc)

        remaining = sum(
            1
            for p in pending
            if self.registry.is_pending(
                p.name, self.settings.max_attempts, self.settings.retry_failed
            )
        )
        return self._finish(started, remaining, notes)

    def _run_one(self, path: Path, index: int, total: int) -> None:
        if self.stop.is_set():
            return
        try:
            record = self._process(path, index, total)
        except DailyQuotaExhausted as exc:
            self.stopped_on_quota = True
            self.stop.set()
            log.warning("Daily quota exhausted at %s", path.name)
            self._note(f"DAILY QUOTA EXHAUSTED  stopped at {path.name}  {exc}")
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Unhandled failure on %s", path.name)
            record = self._record_error(path, exc)

        if record is not None:
            with self._records_lock:
                self.records.append(record.as_dict())

    # ---------------------------------------------------------- per image --

    def _process(self, path: Path, index: int, total: int) -> ItemRecord | None:
        record = ItemRecord(original_name=path.name)
        work: normalize_mod.NormalizeResult | None = None
        prefix = f"[{index}/{total}] {path.name}"

        try:
            # --- exact duplicate, before any expensive work -----------------
            record.sha256 = sha256_file(path)
            with self._dedupe_lock:
                duplicate_of = self.dedupe.exact_match(record.sha256)
                if duplicate_of is None:
                    self.dedupe.add(path.name, record.sha256, None)
            if duplicate_of and duplicate_of != path.name:
                record.duplicate_of = duplicate_of
                return self._finalize_simple(
                    record, path, Status.DUPLICATE,
                    f"Byte-identical to {duplicate_of}.", prefix,
                )

            facts = loader.read_facts(path)
            record.megapixels = round(facts.megapixels, 2)

            # --- resolution gate, before spending an API call ---------------
            if facts.megapixels < self.settings.min_megapixels:
                return self._finalize_simple(
                    record, path, Status.LOW_RESOLUTION,
                    f"{facts.megapixels:.1f}MP is below the "
                    f"{self.settings.min_megapixels}MP minimum.", prefix,
                )

            # --- dry run stays entirely in memory ---------------------------
            # No derived file, no work directory, nothing on disk. That
            # promise is the reason --dry-run is worth trusting.
            if self.settings.dry_run:
                plan = normalize_mod.plan(path, self.settings)
                with loader.open_image(path) as img:
                    dry_quality = quality_mod.analyze_array(img)
                record.quality = dry_quality.as_dict()
                return self._finalize_dry_run(record, path, plan, dry_quality, prefix)

            # --- prepare: normalise, measure, hash --------------------------
            work = normalize_mod.normalize(path, self.settings)
            if work.derived:
                self._note(
                    f"NOTE  {path.name}  {work.note} -> {work.path.name}"
                )

            record.phash = phash_hex(work.path)
            with self._dedupe_lock:
                near = self.dedupe.near_match(record.phash)
                self.dedupe.add(path.name, record.sha256, record.phash)
            if near and near[0] != path.name:
                record.near_duplicate_of = near[0]
                # Informational only: two frames of a burst are often both
                # worth submitting, so this never moves or rejects anything.
                self._note(
                    f"NOTE  {path.name}  visually similar to {near[0]} "
                    f"(distance {near[1]}) - check whether you need both"
                )

            quality = quality_mod.analyze_quality(work.path)
            record.quality = quality.as_dict()

            # --- the API call -----------------------------------------------
            if not self.quota.try_consume():
                raise DailyQuotaExhausted(
                    f"local daily counter reached {self.quota.limit}"
                )
            api_bytes = normalize_mod.encode_for_api(
                work.path, self.settings.api_max_edge
            )
            analysis = self.analyzer.analyze(
                api_bytes, quality_mod.describe_for_prompt(quality)
            )

            decision = choose_status(
                analysis,
                quality,
                min_score=self.settings.min_score,
                min_blur=self.settings.min_blur,
                max_noise=self.settings.max_noise,
                max_clipping=self.settings.max_clipping,
            )

            record.title = analysis.title
            record.score = analysis.commercial_score
            record.risk = analysis.rejection_risk
            record.category = analysis.category
            record.category2 = analysis.category2
            record.keyword_count = len(analysis.keywords)
            record.people_visible = analysis.people_visible
            record.property_or_trademark_visible = analysis.property_or_trademark_visible
            record.watermark_or_overlay_visible = analysis.watermark_or_overlay_visible
            record.status = decision.status
            record.reason = decision.reason

            self._commit(record, path, work, analysis, decision)
            self._log_outcome(record, prefix)
            work = None  # ownership transferred; do not clean up
            return record

        except DailyQuotaExhausted:
            raise
        except (UnsupportedFormatError, ImageDecodeError) as exc:
            return self._record_error(path, exc, record=record, permanent=True, prefix=prefix)
        except Exception as exc:
            return self._record_error(path, exc, record=record, prefix=prefix)
        finally:
            # The single most important cleanup in the program. v4 deleted the
            # derived file on exactly one code path, so every error, every
            # quota stop and every Ctrl-C leaked a full-resolution JPEG into
            # .stockflow_work, and re-runs added more rather than reusing them.
            if work is not None and work.derived:
                try:
                    if work.path.exists() and work.path.parent == self.settings.work_dir:
                        work.path.unlink()
                except OSError as exc:
                    log.debug("Could not remove work file %s: %s", work.path, exc)

    # ------------------------------------------------------------ commits --

    def _commit(
        self,
        record: ItemRecord,
        source: Path,
        work: normalize_mod.NormalizeResult,
        analysis: Any,
        decision: Any,
    ) -> None:
        """Embed metadata, move into place, and record -- crash-safely."""
        status = decision.status
        dest_dir = self.settings.folder / STATUS_FOLDERS.get(status, "06_REVIEW")
        final_name = target_filename(
            analysis.title, source, status, work.actual_format,
            max_chars=self.settings.max_filename_chars,
        )

        # Metadata goes on the file that will actually ship. Anything a buyer
        # might eventually use gets it -- REVIEW and NEEDS_RELEASE files are
        # often uploaded later once checked, and re-deriving metadata then
        # would cost another API call.
        if status in {Status.READY, Status.REVIEW, Status.NEEDS_RELEASE}:
            if work.derived:
                # Restore camera EXIF the marketplaces display, before writing
                # our own tags on top.
                copier = getattr(self.writer, "copy_capture_metadata", None)
                if callable(copier):
                    copier(source, work.path)
            self.writer.write(
                work.path,
                title=analysis.title,
                description=analysis.description,
                keywords=list(analysis.keywords),
            )

        if self.settings.no_move:
            record.final_name = work.path.name
            record.destination = "(left in place)"
            if status == Status.READY:
                self.shutterstock_csv.append(shutterstock_row(record, analysis.keywords))
                self.adobe_csv.append(adobe_row(record, analysis.keywords))
            self.registry.commit(record.original_name, status, **self._commit_fields(record))
            self._bump(status)
            return

        planned = dest_dir / final_name
        self.registry.begin(
            record.original_name, action="move", src=str(work.path), dest=str(planned)
        )

        moved = safe_move(
            work.path, dest_dir, final_name, max_chars=self.settings.max_filename_chars
        )
        record.final_name = moved.name
        record.destination = dest_dir.name

        # A derived file means the original is still sitting in the scan root;
        # archive it so the working folder actually empties out.
        if work.derived and source.exists():
            try:
                safe_move(source, self.settings.folder / FOLDER_SOURCE_ORIGINALS, source.name)
            except Exception as exc:
                log.warning("Could not archive original %s: %s", source.name, exc)

        if status == Status.READY:
            self._warn_marketplace(record, analysis)
            self.shutterstock_csv.append(shutterstock_row(record, analysis.keywords))
            self.adobe_csv.append(adobe_row(record, analysis.keywords))

        self.registry.commit(record.original_name, status, **self._commit_fields(record))
        self._bump(status)

    def _commit_fields(self, record: ItemRecord) -> dict:
        return {
            "attempts": 0,
            "destination": record.destination,
            "final_name": record.final_name,
            "sha256": record.sha256,
            "phash": record.phash,
            "score": record.score,
            "risk": record.risk,
            "category": record.category,
            "category2": record.category2,
            "title": record.title,
            "reason": record.reason,
            "people_visible": record.people_visible,
            "property_or_trademark_visible": record.property_or_trademark_visible,
            "watermark_or_overlay_visible": record.watermark_or_overlay_visible,
            "quality": record.quality,
            "model": self.settings.model,
        }

    def _finalize_simple(
        self, record: ItemRecord, path: Path, status: Status, reason: str, prefix: str
    ) -> ItemRecord:
        """Route a file that never reached the model (duplicate, low-res)."""
        record.status = status
        record.reason = reason

        if self.settings.dry_run or self.settings.no_move:
            record.final_name = path.name
            record.destination = (
                "(dry run)" if self.settings.dry_run else "(left in place)"
            )
        else:
            dest_dir = self.settings.folder / STATUS_FOLDERS[status]
            # Keep the real extension: these files are never converted.
            name = target_filename(
                "", path, status, path.suffix.lstrip(".").upper(),
                max_chars=self.settings.max_filename_chars,
            )
            planned = dest_dir / name
            self.registry.begin(
                record.original_name, action="move", src=str(path), dest=str(planned)
            )
            moved = safe_move(
                path, dest_dir, name, max_chars=self.settings.max_filename_chars
            )
            record.final_name = moved.name
            record.destination = dest_dir.name

        if not self.settings.dry_run:
            self.registry.commit(record.original_name, status, **self._commit_fields(record))
        self._bump(status)
        self._note(f"{status}  {record.original_name}  {reason}")
        self.progress(f"{prefix} ... {status} - {reason}")
        return record

    def _finalize_dry_run(
        self, record: ItemRecord, path: Path, plan: str, quality: Any, prefix: str
    ) -> ItemRecord:
        record.status = None
        bits = [f"{record.megapixels}MP"]
        if plan:
            bits.append(plan)
        if quality.measured:
            bits.append(f"focus {quality.blur_score:.0f}")
            bits.append(f"noise {quality.noise_score:.1f}")
            if quality.flags:
                bits.append("flags: " + ", ".join(quality.flags))
        record.reason = "would call the model; " + "; ".join(bits)
        record.destination = "(dry run)"
        self.progress(f"{prefix} ... would analyse - {'; '.join(bits)}")
        return record

    def _record_error(
        self,
        path: Path,
        exc: Exception,
        *,
        record: ItemRecord | None = None,
        permanent: bool = False,
        prefix: str = "",
    ) -> ItemRecord:
        record = record or ItemRecord(original_name=path.name)
        attempts = self.registry.attempts_of(path.name) + 1
        status = (
            Status.ERROR_PERMANENT
            if permanent or attempts >= self.settings.max_attempts
            else Status.ERROR
        )
        record.status = status
        record.error = str(exc)
        record.reason = f"Failed after {attempts} attempt(s): {exc}"

        if not self.settings.dry_run:
            # A permanently failed file gets a home. v4 left it in the scan
            # root with a terminal status, so it was skipped forever while
            # still cluttering the folder with no visible explanation.
            if status is Status.ERROR_PERMANENT and not self.settings.no_move and path.exists():
                try:
                    dest_dir = self.settings.folder / STATUS_FOLDERS[status]
                    planned = dest_dir / path.name
                    self.registry.begin(
                        path.name, action="move", src=str(path), dest=str(planned)
                    )
                    moved = safe_move(path, dest_dir, path.name)
                    record.final_name = moved.name
                    record.destination = dest_dir.name
                except Exception as move_exc:
                    log.warning("Could not move failed file %s: %s", path.name, move_exc)
                    self.registry.abandon(path.name)

            self.registry.commit(
                path.name,
                status,
                attempts=attempts,
                last_error=str(exc)[:500],
                sha256=record.sha256,
                destination=record.destination,
                final_name=record.final_name,
            )

        self._bump(status)
        self._note(f"ERROR  {path.name}  attempt={attempts}  {exc}")
        self.progress(f"{prefix or path.name} ... ERROR ({attempts}/{self.settings.max_attempts}) - {exc}")
        return record

    # ------------------------------------------------------------ helpers --

    def _warn_marketplace(self, record: ItemRecord, analysis: Any) -> None:
        """Flag anything a marketplace would object to, without blocking it.

        These are submission-format concerns rather than quality judgements,
        so they are surfaced for the photographer to act on rather than used
        to reroute the file.
        """
        from . import marketplaces

        problems = (
            marketplaces.filename_warnings(record.final_name)
            + marketplaces.keyword_warnings(len(analysis.keywords))
            + marketplaces.title_warnings(analysis.title)
        )
        if not marketplaces.adobe_category_number(record.category):
            problems.append(
                f"Shutterstock category {record.category!r} has no Adobe Stock "
                f"equivalent; the Adobe CSV leaves it blank for Adobe to suggest"
            )
        for problem in problems:
            self._note(f"CHECK  {record.original_name}  {problem}")

    def _note(self, line: str) -> None:
        """Append to the review log, unless this is a dry run.

        A dry run must not create or touch a single file -- that promise is
        the whole reason to trust it before letting the tool loose on a folder
        of originals.
        """
        if self.settings.dry_run:
            return
        self.review_log.write(line)

    def _bump(self, status: Status) -> None:
        with self._counts_lock:
            self.counts[status] = self.counts.get(status, 0) + 1

    def _log_outcome(self, record: ItemRecord, prefix: str) -> None:
        status = record.status
        if status is not Status.READY:
            self._note(
                f"{status}  {record.original_name}  score={record.score}  "
                f"risk={record.risk}  {record.reason}"
            )
        if status is Status.NEEDS_RELEASE:
            if record.people_visible:
                self._note(f"FLAG  {record.original_name}  PEOPLE - model release needed")
            if record.property_or_trademark_visible:
                self._note(
                    f"FLAG  {record.original_name}  PROPERTY/TRADEMARK - release may be needed"
                )
        if record.watermark_or_overlay_visible:
            self._note(
                f"FLAG  {record.original_name}  WATERMARK/OVERLAY - every marketplace auto-rejects this"
            )
        self.progress(
            f"{prefix} ... {status}  score={record.score}/100  "
            f"risk={record.risk}  keywords={record.keyword_count}"
        )

    def _finish(self, started: float, remaining: int, notes: list[str]) -> RunResult:
        api_stats = getattr(self.analyzer, "stats", {}) or {}
        summary = build_summary(
            self.records,
            counts=self.counts,
            remaining=remaining,
            duration=time.time() - started,
            settings=self.settings,
            api_stats=api_stats,
            quota_used=self.quota.used,
            quota_limit=self.quota.limit,
            stopped_on_quota=self.stopped_on_quota,
        )
        summary["rate_limit_source"] = self.model_limits.source
        summary["adaptive_pauses"] = self.gate.trips

        paths: dict[str, Path] = {}
        if not self.settings.dry_run:
            paths = write_report(self.settings.reports_dir, self.records, summary)
            try:
                self.registry.save(force=True)
            except Exception as exc:
                log.error("Could not save the registry: %s", exc)
            self._cleanup_work_dir()

        return RunResult(
            records=self.records,
            counts=self.counts,
            summary=summary,
            report_paths=paths,
            stopped_on_quota=self.stopped_on_quota,
            reconcile_notes=notes,
        )

    def _cleanup_work_dir(self) -> None:
        work = self.settings.work_dir
        if not work.exists():
            return
        try:
            leftovers = list(work.iterdir())
            if not leftovers:
                work.rmdir()
            else:
                log.debug("%d file(s) left in %s", len(leftovers), work.name)
        except OSError:
            pass
