"""Local quality measurement.

The thresholds are calibration guesses, so these tests assert on *relative
ordering and behaviour*, not on absolute magic numbers -- otherwise every
future tuning change would break the suite for no good reason.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageFilter

from stockflow.imaging import quality as q
from tests.conftest import checkerboard


@pytest.fixture(scope="module")
def sharp():
    return checkerboard(1200, 900)


@pytest.fixture(scope="module")
def blurry(sharp):
    return sharp.filter(ImageFilter.GaussianBlur(6))


class TestFocus:
    def test_sharp_scores_far_above_blurry(self, sharp, blurry):
        assert q.analyze_array(sharp).blur_score > q.analyze_array(blurry).blur_score * 10

    def test_blurry_is_flagged_soft(self, blurry):
        assert "soft" in q.analyze_array(blurry).flags

    def test_sharp_is_not_flagged(self, sharp):
        assert "soft" not in q.analyze_array(sharp).flags

    def test_shallow_depth_of_field_is_not_called_soft(self, sharp, blurry):
        """A sharp subject against a soft background is good stock, not a
        reject. Mean sharpness would punish exactly the bokeh work that sells,
        which is why the score is a high percentile across tiles."""
        arr = np.asarray(blurry).copy()
        sharp_arr = np.asarray(sharp)
        arr[300:600, 400:800] = sharp_arr[300:600, 400:800]
        report = q.analyze_array(Image.fromarray(arr))
        assert "soft" not in report.flags

    def test_focus_score_is_scale_stable(self):
        """Variance-of-Laplacian measured naively is ~53x larger at 24MP than
        at 1024px for the same photo. Fixed-size native-resolution tiles are
        what make one threshold usable across a mixed portfolio."""
        small = q.analyze_array(checkerboard(900, 700)).blur_score
        large = q.analyze_array(checkerboard(2400, 1800)).blur_score
        assert 0.4 < (small / large) < 2.5, f"scores diverged: {small} vs {large}"


class TestNoise:
    def test_noise_is_detected(self, sharp):
        rng = np.random.default_rng(1)
        arr = np.asarray(sharp, dtype=np.int16) + rng.normal(0, 20, np.asarray(sharp).shape)
        noisy = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        assert q.analyze_array(noisy).noise_score > q.analyze_array(sharp).noise_score
        assert "noisy" in q.analyze_array(noisy).flags

    def test_clean_image_is_not_flagged_noisy(self, sharp):
        assert "noisy" not in q.analyze_array(sharp).flags


class TestExposure:
    def test_blown_highlights_detected(self):
        img = Image.fromarray(np.full((600, 800, 3), 255, dtype=np.uint8))
        report = q.analyze_array(img)
        assert report.clip_high > 0.9
        assert "blown-highlights" in report.flags

    def test_crushed_shadows_detected(self):
        img = Image.fromarray(np.zeros((600, 800, 3), dtype=np.uint8))
        report = q.analyze_array(img)
        assert report.clip_low > 0.9
        assert "crushed-shadows" in report.flags

    def test_well_exposed_image_is_clean(self, sharp):
        report = q.analyze_array(sharp)
        assert report.clip_low < 0.05 and report.clip_high < 0.05

    def test_high_key_is_noted_not_flagged(self):
        """High-key and low-key are legitimate styles and must never be
        auto-rejected on brightness alone."""
        img = Image.fromarray(np.full((600, 800, 3), 225, dtype=np.uint8))
        report = q.analyze_array(img)
        assert "overexposed" not in report.flags


class TestRobustness:
    def test_tiny_image_does_not_crash(self):
        assert q.analyze_array(Image.new("RGB", (8, 8), "red")).measured

    def test_greyscale_is_handled(self):
        assert q.analyze_array(Image.new("L", (300, 300), 128).convert("RGB")).measured

    def test_missing_file_returns_unmeasured_rather_than_raising(self, tmp_path):
        """Quality analysis is a signal, never a gate -- it must not be able
        to break a run."""
        report = q.analyze_quality(tmp_path / "does-not-exist.jpg")
        assert report.measured is False

    def test_unmeasured_report_is_serialisable(self):
        assert isinstance(q.unmeasured("because").as_dict(), dict)


class TestPromptDescription:
    def test_measured_report_produces_text(self, sharp):
        text = q.describe_for_prompt(q.analyze_array(sharp))
        assert "focus score" in text and "noise sigma" in text

    def test_unmeasured_report_produces_nothing(self):
        assert q.describe_for_prompt(q.unmeasured()) == ""

    def test_tells_the_model_to_trust_the_numbers(self, sharp):
        """The model is shown a downsampled preview where sharpness and noise
        no longer exist, so it must be told to defer to the measurements."""
        assert "Trust these figures" in q.describe_for_prompt(q.analyze_array(sharp))
