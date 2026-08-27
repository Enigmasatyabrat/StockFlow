"""Calibration: distributions, suggested cuts, and honest non-separation."""

from __future__ import annotations

import pytest
from PIL import ImageFilter

from stockflow import calibrate as cal
from stockflow.models import QualityReport


def report(blur=500.0, noise=1.0, clip_low=0.0, clip_high=0.0):
    return QualityReport(
        blur_score=blur, noise_score=noise, clip_low=clip_low, clip_high=clip_high,
        mean_luma=128.0, contrast=50.0,
    )


def measurements(values, attr="blur"):
    from pathlib import Path

    out = []
    for i, v in enumerate(values):
        kw = {attr if attr != "clip" else "clip_high": v}
        out.append(cal.Measurement(Path(f"{i}.jpg"), report(**kw)))
    return out


class TestPercentiles:
    def test_basic(self):
        p = cal.percentiles([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        assert p[0] == 0 and p[100] == 100 and p[50] == 50

    def test_empty_is_safe(self):
        assert cal.percentiles([])[50] == 0.0

    def test_single_value(self):
        p = cal.percentiles([7.0])
        assert p[0] == p[50] == p[100] == 7.0


class TestDistribution:
    def test_empty(self):
        assert "No images" in cal.describe_distribution([])

    def test_reports_count_and_suggestion(self):
        text = cal.describe_distribution(measurements([100, 200, 300, 400, 500]))
        assert "Measured 5 image(s)" in text
        assert "--min-blur" in text

    def test_states_that_suggestions_are_descriptive(self):
        """The whole point is not to hand over a number that looks
        authoritative when it is only describing this particular pile."""
        text = cal.describe_distribution(measurements([100, 200, 300]))
        assert "descriptive, not prescriptive" in text

    def test_shows_the_builtin_defaults_for_comparison(self):
        assert "built-in defaults" in cal.describe_distribution(measurements([100, 200]))

    def test_warns_when_defaults_would_flag_most_images(self):
        """If the shipped default would reject nearly everything, say so
        instead of letting the user adopt it silently."""
        text = cal.describe_distribution(measurements([5, 6, 7, 8, 9, 10]))
        assert "clearly too high" in text

    def test_no_warning_when_defaults_are_reasonable(self):
        text = cal.describe_distribution(measurements([500, 600, 700, 800]))
        assert "clearly too high" not in text


class TestWorst:
    def test_worst_by_focus_is_ascending(self):
        worst = cal.worst_by_focus(measurements([500, 100, 300, 50]), count=2)
        assert [m.report.blur_score for m in worst] == [50, 100]

    def test_worst_by_noise_is_descending(self):
        worst = cal.worst_by_noise(measurements([1, 9, 3], attr="noise"), count=2)
        assert [m.report.noise_score for m in worst] == [9, 3]

    def test_count_is_respected(self):
        assert len(cal.worst_by_focus(measurements(range(20)), count=3)) == 3


class TestBestThreshold:
    def test_separates_cleanly_when_the_sets_are_apart(self):
        result = cal.best_threshold(
            [500, 600, 700], [10, 20, 30], higher_is_better=True, metric="focus"
        )
        assert result.usable
        assert result.accuracy == 1.0
        assert 30 < result.threshold <= 500

    def test_lower_is_better_direction(self):
        result = cal.best_threshold(
            [1.0, 1.5, 2.0], [8.0, 9.0], higher_is_better=False, metric="noise"
        )
        assert result.usable
        assert 2.0 <= result.threshold < 8.0

    def test_identical_sets_do_not_separate(self):
        """Reporting overlap honestly beats returning a confident number for
        two sets no single threshold can split."""
        values = [100, 200, 300]
        result = cal.best_threshold(values, values, higher_is_better=True, metric="focus")
        assert not result.usable

    def test_heavily_overlapping_sets_do_not_separate(self):
        result = cal.best_threshold(
            [100, 200, 300, 400], [150, 250, 350, 450],
            higher_is_better=True, metric="focus",
        )
        assert not result.usable

    def test_empty_input_is_safe(self):
        assert not cal.best_threshold([], [1, 2], higher_is_better=True, metric="x").usable
        assert not cal.best_threshold([1, 2], [], higher_is_better=True, metric="x").usable


class TestSeparationReport:
    def test_needs_both_sides(self):
        assert "both folders" in cal.describe_separation(measurements([1]), [])

    def test_reports_a_clean_split(self):
        good = measurements([500, 600, 700])
        bad = measurements([10, 20, 30])
        text = cal.describe_separation(good, bad)
        assert "Suggested:" in text
        assert "--min-blur" in text

    def test_names_metrics_that_do_not_separate(self):
        good = measurements([500, 600, 700])
        bad = measurements([10, 20, 30])
        text = cal.describe_separation(good, bad)
        # noise and clipping are identical across both sets here
        assert "do NOT separate" in text
        assert "real answer, not a failure" in text

    def test_all_overlapping_advises_leaving_gates_off(self):
        same = measurements([100, 200, 300])
        text = cal.describe_separation(same, measurements([100, 200, 300]))
        assert "Keep all the local gates off" in text


class TestMeasureFolder:
    def test_measures_real_images(self, settings, make_image):
        for i in range(3):
            make_image(f"p{i}.jpg", folder=settings.folder)
        found = cal.measure_folder(settings.folder, settings)
        assert len(found) == 3
        assert all(m.report.measured for m in found)

    def test_respects_limit(self, settings, make_image):
        for i in range(5):
            make_image(f"p{i}.jpg", folder=settings.folder)
        assert len(cal.measure_folder(settings.folder, settings, limit=2)) == 2

    def test_unreadable_files_are_skipped_not_fatal(self, settings, make_image):
        make_image("good.jpg", folder=settings.folder)
        (settings.folder / "broken.jpg").write_bytes(b"not an image")
        found = cal.measure_folder(settings.folder, settings)
        assert len(found) == 1

    def test_empty_folder(self, settings):
        assert cal.measure_folder(settings.folder, settings) == []

    def test_blurry_scores_below_sharp(self, settings, make_image):
        make_image("sharp.jpg", folder=settings.folder)
        make_image("soft.jpg", blur=6, folder=settings.folder)
        found = {m.name: m.report.blur_score for m in cal.measure_folder(settings.folder, settings)}
        assert found["soft.jpg"] < found["sharp.jpg"]


class TestCli:
    def test_calibrate_needs_no_api_key(self, photo_folder, make_image, monkeypatch, capsys):
        """Calibration reads pixels and nothing else, so it must not demand a
        key or an exiftool binary."""
        from stockflow.cli import main

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        make_image("a.jpg", folder=photo_folder)
        assert main([str(photo_folder), "--calibrate", "--min-megapixels", "0.1"]) == 0
        out = capsys.readouterr().out
        assert "calibration" in out
        assert "--min-blur" in out

    def test_calibrate_moves_nothing(self, photo_folder, make_image, monkeypatch):
        from stockflow.cli import main

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        make_image("a.jpg", folder=photo_folder)
        before = sorted(p.name for p in photo_folder.iterdir())
        main([str(photo_folder), "--calibrate", "--min-megapixels", "0.1"])
        assert sorted(p.name for p in photo_folder.iterdir()) == before

    def test_against_folder_must_exist(self, photo_folder, make_image, monkeypatch, capsys):
        from stockflow.cli import main

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        make_image("a.jpg", folder=photo_folder)
        code = main([str(photo_folder), "--calibrate", "--min-megapixels", "0.1",
                     "--against", str(photo_folder / "nope")])
        assert code == 2
