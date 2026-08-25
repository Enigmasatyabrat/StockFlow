"""End-to-end pipeline behaviour, driven entirely by fakes.

These are the tests that matter most: the pipeline relocates a photographer's
original files, so every failure mode here is potential data loss.
"""

from __future__ import annotations

import json
import threading

import pytest

from stockflow.analyzer import FakeAnalyzer
from stockflow.errors import DailyQuotaExhausted, MetadataWriteError
from stockflow.metadata import FakeMetadataWriter
from stockflow.models import (
    FOLDER_DUPLICATES,
    FOLDER_ERRORS,
    FOLDER_LOWRES,
    FOLDER_NEEDS_RELEASE,
    FOLDER_READY,
    FOLDER_REVIEW,
    FOLDER_SOURCE_ORIGINALS,
    Status,
)
from stockflow.pipeline import Pipeline
from stockflow.registry import Registry


def run(settings, analyzer=None, writer=None, **kw):
    pipeline = Pipeline(settings, analyzer or FakeAnalyzer(), writer or FakeMetadataWriter(), **kw)
    return pipeline, pipeline.run()


class TestHappyPath:
    def test_ready_image_is_moved_and_recorded(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        pipeline, result = run(settings)

        ready = list((settings.folder / FOLDER_READY).glob("*.jpg"))
        assert len(ready) == 1
        assert not (settings.folder / "photo.jpg").exists()
        assert result.counts[Status.READY] == 1
        assert pipeline.registry.status_of("photo.jpg") is Status.READY

    def test_csv_row_written_for_ready(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        run(settings)
        csv_path = settings.reports_dir / "shutterstock_upload.csv"
        lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == "Filename,Description,Keywords,Categories"
        assert len(lines) == 2

    def test_csv_header_written_once_across_runs(self, settings, make_image):
        make_image("a.jpg", folder=settings.folder)
        run(settings)
        make_image("b.jpg", folder=settings.folder)
        run(settings)
        lines = (settings.reports_dir / "shutterstock_upload.csv").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        assert lines.count("Filename,Description,Keywords,Categories") == 1
        assert len(lines) == 3

    def test_metadata_written_before_move(self, settings, make_image):
        writer = FakeMetadataWriter()
        make_image("photo.jpg", folder=settings.folder)
        run(settings, writer=writer)
        assert len(writer.written) == 1
        assert writer.written[0]["title"]
        assert len(writer.written[0]["keywords"]) > 0

    def test_report_files_are_produced(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        _, result = run(settings)
        assert result.report_paths["latest_json"].exists()
        payload = json.loads(result.report_paths["latest_json"].read_text(encoding="utf-8"))
        assert payload["summary"]["ready_to_upload"] == 1
        assert len(payload["items"]) == 1

    def test_earlier_reports_are_not_destroyed(self, settings, make_image):
        """v4 wrote report.json in truncate mode, so each run destroyed the
        previous run's audit trail -- and a big folder always takes several."""
        make_image("a.jpg", folder=settings.folder)
        _, first = run(settings)
        make_image("b.jpg", folder=settings.folder)
        _, second = run(settings)
        assert first.report_paths["run_json"].exists()
        assert second.report_paths["run_json"].exists()
        assert first.report_paths["run_json"] != second.report_paths["run_json"]


class TestRouting:
    def test_low_resolution_is_set_aside(self, settings, make_image):
        make_image("small.jpg", width=800, height=600, folder=settings.folder)
        _, result = run(settings)
        assert result.counts[Status.LOW_RESOLUTION] == 1
        assert list((settings.folder / FOLDER_LOWRES).glob("*.jpg"))

    def test_low_res_never_calls_the_model(self, settings, make_image):
        analyzer = FakeAnalyzer()
        make_image("small.jpg", width=800, height=600, folder=settings.folder)
        run(settings, analyzer=analyzer)
        assert analyzer.calls == [], "quota must not be spent on a file we already reject"

    def test_exact_duplicate_detected(self, settings, make_image):
        first = make_image("a.jpg", folder=settings.folder)
        (settings.folder / "b.jpg").write_bytes(first.read_bytes())
        _, result = run(settings)
        assert result.counts[Status.DUPLICATE] == 1
        assert list((settings.folder / FOLDER_DUPLICATES).glob("*.jpg"))

    def test_duplicate_detected_across_runs(self, settings, make_image):
        original = make_image("a.jpg", folder=settings.folder)
        data = original.read_bytes()
        run(settings)
        (settings.folder / "later.jpg").write_bytes(data)
        _, second = run(settings)
        assert second.counts[Status.DUPLICATE] == 1

    def test_people_route_to_needs_release(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(default=FakeAnalyzer.sample(people_visible=True))
        _, result = run(settings, analyzer=analyzer)
        assert result.counts[Status.NEEDS_RELEASE] == 1
        assert list((settings.folder / FOLDER_NEEDS_RELEASE).glob("*.jpg"))

    def test_risky_routes_to_review(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(
            default=FakeAnalyzer.sample(rejection_risk="High", rejection_reason="Soft focus")
        )
        _, result = run(settings, analyzer=analyzer)
        assert result.counts[Status.REVIEW] == 1
        assert list((settings.folder / FOLDER_REVIEW).glob("*.jpg"))

    def test_non_ready_is_absent_from_upload_csv(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(default=FakeAnalyzer.sample(commercial_score=10))
        run(settings, analyzer=analyzer)
        assert not (settings.reports_dir / "shutterstock_upload.csv").exists()


class TestConversion:
    def test_png_is_converted_and_original_archived(self, settings, make_image):
        make_image("photo.png", folder=settings.folder)
        run(settings)
        assert list((settings.folder / FOLDER_READY).glob("*.jpg"))
        assert (settings.folder / FOLDER_SOURCE_ORIGINALS / "photo.png").exists()

    def test_no_work_files_left_behind(self, settings, make_image):
        make_image("photo.png", folder=settings.folder)
        run(settings)
        work = settings.work_dir
        assert not work.exists() or not list(work.iterdir())

    def test_work_file_removed_even_when_analysis_fails(self, settings, make_image):
        """v4 deleted the derived file on exactly one code path, so every
        error leaked a full-resolution JPEG into .stockflow_work."""
        make_image("photo.png", folder=settings.folder)
        analyzer = FakeAnalyzer(errors=[RuntimeError("model exploded")])
        run(settings, analyzer=analyzer)
        work = settings.work_dir
        assert not work.exists() or not list(work.iterdir())


class TestErrors:
    def test_failure_is_recorded_and_retried(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(errors=[RuntimeError("boom")])
        pipeline, result = run(settings, analyzer=analyzer)
        assert result.counts[Status.ERROR] == 1
        assert pipeline.registry.is_pending("photo.jpg", max_attempts=3)

    def test_file_stays_put_on_a_retryable_error(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(errors=[RuntimeError("boom")])
        run(settings, analyzer=analyzer)
        assert (settings.folder / "photo.jpg").exists(), "a retryable file must remain scannable"

    def test_permanent_failure_gets_a_home(self, settings, make_image):
        """v4 left ERROR_PERMANENT files in the scan root with a terminal
        status, so they were skipped forever while still cluttering the folder."""
        make_image("photo.jpg", folder=settings.folder)
        s = settings.with_(max_attempts=1)
        analyzer = FakeAnalyzer(errors=[RuntimeError("boom")])
        _, result = run(s, analyzer=analyzer)
        assert result.counts[Status.ERROR_PERMANENT] == 1
        assert list((s.folder / FOLDER_ERRORS).glob("*.jpg"))

    def test_metadata_failure_does_not_move_the_file(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        writer = FakeMetadataWriter(fail_on={"photo.jpg"})
        _, result = run(settings, writer=writer)
        assert result.counts[Status.ERROR] == 1
        assert (settings.folder / "photo.jpg").exists()
        assert not list((settings.folder / FOLDER_READY).glob("*"))

    def test_malformed_model_output_is_an_error_not_a_crash(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(responses=[{"title": "no keywords here"}])
        _, result = run(settings, analyzer=analyzer)
        assert result.counts[Status.ERROR] == 1

    def test_unreadable_file_fails_permanently(self, settings):
        (settings.folder / "broken.jpg").write_bytes(b"not an image at all")
        _, result = run(settings)
        assert result.counts[Status.ERROR_PERMANENT] == 1


class TestQuota:
    def test_daily_quota_stops_the_run_cleanly(self, settings, make_image):
        for i in range(3):
            make_image(f"p{i}.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(errors=[DailyQuotaExhausted("out of quota")])
        pipeline, result = run(settings, analyzer=analyzer)
        assert result.stopped_on_quota
        assert result.summary["remaining"] >= 1

    def test_quota_exhaustion_does_not_blame_the_photo(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        analyzer = FakeAnalyzer(errors=[DailyQuotaExhausted("out of quota")])
        pipeline, _ = run(settings, analyzer=analyzer)
        assert pipeline.registry.status_of("photo.jpg") is not Status.ERROR
        assert pipeline.registry.is_pending("photo.jpg", max_attempts=3)
        assert (settings.folder / "photo.jpg").exists()

    def test_run_is_trimmed_to_remaining_quota(self, settings, make_image):
        for i in range(5):
            make_image(f"p{i}.jpg", folder=settings.folder)
        s = settings.with_(rpd=2)
        analyzer = FakeAnalyzer()
        _, result = run(s, analyzer=analyzer)
        assert len(analyzer.calls) <= 2


class TestDryRun:
    def test_touches_absolutely_nothing(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        make_image("small.jpg", width=800, height=600, folder=settings.folder)
        before = sorted(p.name for p in settings.folder.iterdir())

        s = settings.with_(dry_run=True)
        analyzer = FakeAnalyzer()
        _, result = run(s, analyzer=analyzer)

        assert sorted(p.name for p in settings.folder.iterdir()) == before
        assert analyzer.calls == [], "a dry run must not spend API quota"
        assert len(result.records) == 2

    def test_still_reports_what_would_happen(self, settings, make_image):
        make_image("small.jpg", width=800, height=600, folder=settings.folder)
        s = settings.with_(dry_run=True)
        _, result = run(s)
        assert result.records[0]["status"] == str(Status.LOW_RESOLUTION)


class TestResume:
    def test_processed_files_are_not_redone(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        run(settings)
        analyzer = FakeAnalyzer()
        _, second = run(settings, analyzer=analyzer)
        assert analyzer.calls == []
        assert len(second.records) == 0

    def test_batch_limit_leaves_the_rest_pending(self, settings, make_image):
        for i in range(5):
            make_image(f"p{i}.jpg", folder=settings.folder)
        s = settings.with_(batch_limit=2)
        _, result = run(s)
        assert len(result.records) == 2
        assert result.summary["remaining"] == 3

    def test_interrupted_move_is_reconciled_next_run(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        registry = Registry.load(settings.folder)
        dest = settings.folder / FOLDER_READY / "moved.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"already there")
        registry.begin("photo.jpg", action="move",
                       src=str(settings.folder / "gone.jpg"), dest=str(dest))

        _, result = run(settings)
        assert result.reconcile_notes
        assert "move had completed" in result.reconcile_notes[0]


class TestConcurrency:
    def test_identical_titles_do_not_overwrite_each_other(self, settings, make_image):
        """Filenames come from slugified model titles, so near-duplicate images
        reliably collide. Losing one to an overwrite would be silent data loss."""
        for i in range(6):
            make_image(f"p{i}.jpg", width=1200 + i, height=1000, folder=settings.folder)
        s = settings.with_(workers=4, min_megapixels=0.5)
        _, result = run(s)
        ready = list((s.folder / FOLDER_READY).glob("*.jpg"))
        assert len(ready) == 6, "every image must survive"
        assert len({p.name for p in ready}) == 6

    def test_registry_consistent_under_workers(self, settings, make_image):
        for i in range(8):
            make_image(f"p{i}.jpg", width=1200, height=1000, folder=settings.folder)
        s = settings.with_(workers=4, min_megapixels=0.5)
        pipeline, _ = run(s)
        reloaded = Registry.load(s.folder)
        assert len(reloaded.items) == 8
        assert all("intent" not in v for v in reloaded.items.values())


class TestNoMove:
    def test_files_stay_in_place(self, settings, make_image):
        make_image("photo.jpg", folder=settings.folder)
        s = settings.with_(no_move=True)
        _, result = run(s)
        assert (s.folder / "photo.jpg").exists()
        assert result.counts[Status.READY] == 1
