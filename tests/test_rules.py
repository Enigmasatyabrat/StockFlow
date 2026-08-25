"""Policy layer. Pure functions, so no fixtures needed.

Most of these are regression tests for specific v4 misbehaviours; each one
names the failure it guards against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stockflow.models import Analysis, QualityReport, Status
from stockflow.rules import (
    as_bool,
    as_int,
    as_text,
    choose_status,
    clean_keywords,
    normalize_category,
    normalize_risk,
    slugify,
    target_filename,
)


# ------------------------------------------------------------------ coercion --

class TestAsBool:
    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "no", "0", "", "n"])
    def test_falsy_strings_are_false(self, value):
        """bool("false") is True in Python. That trap routed clean images into
        NEEDS_RELEASE and marked good photos as watermarked."""
        assert as_bool(value) is False

    @pytest.mark.parametrize("value", ["true", "True", "yes", "1", "y"])
    def test_truthy_strings(self, value):
        assert as_bool(value) is True

    def test_real_bools_pass_through(self):
        assert as_bool(True) is True
        assert as_bool(False) is False

    def test_none_uses_default(self):
        assert as_bool(None) is False
        assert as_bool(None, default=True) is True

    def test_unrecognised_string_uses_default(self):
        assert as_bool("maybe", default=True) is True
        assert as_bool("maybe", default=False) is False


class TestAsInt:
    @pytest.mark.parametrize(
        "value,expected",
        [(85, 85), ("85", 85), (85.4, 85), ("85/100", 85), ("score: 72", 72), (None, 0)],
    )
    def test_parsing(self, value, expected):
        assert as_int(value) == expected

    def test_clamped_to_range(self):
        assert as_int(150) == 100
        assert as_int(-20) == 0

    def test_garbage_uses_default(self):
        assert as_int("not a number", default=50) == 50


class TestAsText:
    def test_json_null_becomes_empty_string(self):
        """v4 called .strip() straight on the value; a JSON null raised
        AttributeError and cost three billable retries per affected image."""
        assert as_text(None) == ""

    def test_strips(self):
        assert as_text("  hello  ") == "hello"


class TestNormalizeRisk:
    @pytest.mark.parametrize("value", ["High", "HIGH", "high", " high "])
    def test_case_insensitive(self, value):
        """v4 compared against {"Medium","High"} exactly, so "HIGH" fell
        through to READY -- the opposite of the intent."""
        assert normalize_risk(value) == "High"

    def test_unknown_is_flagged(self):
        assert normalize_risk("banana") == "Unknown"
        assert normalize_risk(None) == "Unknown"


class TestNormalizeCategory:
    def test_valid_passes(self):
        cat, cat2, warnings = normalize_category("Nature", "Abstract")
        assert (cat, cat2, warnings) == ("Nature", "Abstract", [])

    def test_case_insensitive_match(self):
        cat, _, _ = normalize_category("nature")
        assert cat == "Nature"

    def test_invalid_falls_back(self):
        cat, _, warnings = normalize_category("Landscapes")
        assert cat == "Miscellaneous"
        assert warnings

    def test_duplicate_secondary_dropped(self):
        _, cat2, _ = normalize_category("Nature", "Nature")
        assert cat2 == ""


# ------------------------------------------------------------------ keywords --

class TestCleanKeywords:
    def test_splits_on_commas(self):
        """A keyword containing a comma is indistinguishable from two keywords
        once written to IPTC, and corrupts the comma-joined CSV column."""
        assert clean_keywords(["black, white", "sky"]) == ["black", "white", "sky"]

    def test_dedupes_case_insensitively(self):
        assert clean_keywords(["Sky", "sky", "SKY"]) == ["Sky"]

    def test_collapses_plurals(self):
        assert clean_keywords(["cloud", "clouds"]) == ["cloud"]

    def test_caps_each_keyword_at_iptc_byte_limit(self):
        long = "x" * 100
        result = clean_keywords([long])
        assert len(result[0].encode("utf-8")) <= 64

    def test_does_not_split_multibyte_characters(self):
        result = clean_keywords(["日" * 40])
        assert result[0].encode("utf-8").decode("utf-8")  # still valid UTF-8

    def test_caps_list_length(self):
        assert len(clean_keywords([f"kw{i}" for i in range(200)])) == 50

    def test_collapses_internal_whitespace(self):
        assert clean_keywords(["sunset  beach", "sunset beach"]) == ["sunset beach"]

    def test_drops_too_short_and_empty(self):
        assert clean_keywords(["a", "", "  ", None, "ok"]) == ["ok"]


# ----------------------------------------------------------------- filenames --

class TestSlugify:
    def test_basic(self):
        assert slugify("Woman Drinking Coffee") == "woman-drinking-coffee"

    def test_strips_punctuation(self):
        assert slugify("Hello, World! (2024)") == "hello-world-2024"

    def test_keeps_unicode_letters(self):
        assert slugify("日本の桜") == "日本の桜"

    def test_empty_falls_back(self):
        assert slugify("") == "image"
        assert slugify("!!!") == "image"

    def test_windows_reserved_names_avoided(self):
        assert slugify("CON") != "con"
        assert slugify("nul") != "nul"

    def test_no_trailing_dot_or_space(self):
        assert not slugify("trailing dot.").endswith((".", " ", "-"))

    def test_respects_max_length(self):
        assert len(slugify("word " * 60, max_len=40)) <= 40


class TestTargetFilename:
    def test_tiff_keeps_tiff_extension(self):
        """v4 hardcoded .jpg for READY, so an untouched sub-50MB TIFF was
        renamed to .jpg while still containing TIFF bytes."""
        name = target_filename("A Photo", Path("orig.tif"), Status.READY, "TIFF")
        assert name == "a-photo.tif"

    def test_converted_file_gets_jpg(self):
        name = target_filename("A Photo", Path("orig.png"), Status.READY, "JPEG")
        assert name == "a-photo.jpg"

    def test_falls_back_to_original_stem_without_title(self):
        assert target_filename("", Path("DSC_0001.jpg"), Status.DUPLICATE, "JPEG").startswith(
            "dsc-0001"
        )


# ------------------------------------------------------------------ routing --

def make_analysis(**kw) -> Analysis:
    base = dict(
        title="A photo",
        description="Description.",
        keywords=("a", "b"),
        category="Nature",
        commercial_score=80,
        rejection_risk="Low",
    )
    base.update(kw)
    return Analysis(**base)


def make_quality(**kw) -> QualityReport:
    base = dict(
        blur_score=500.0, noise_score=1.0, clip_low=0.0, clip_high=0.0,
        mean_luma=128.0, contrast=50.0,
    )
    base.update(kw)
    return QualityReport(**base)


class TestChooseStatus:
    def test_clean_image_is_ready(self):
        assert choose_status(make_analysis(), make_quality()).status is Status.READY

    def test_low_score_is_low_quality(self):
        d = choose_status(make_analysis(commercial_score=30), make_quality(), min_score=60)
        assert d.status is Status.LOW_QUALITY
        assert "30" in d.reason

    def test_people_need_release(self):
        d = choose_status(make_analysis(people_visible=True), make_quality())
        assert d.status is Status.NEEDS_RELEASE

    def test_release_outranks_watermark(self):
        """A watermarked photo of an identifiable person is still a release
        problem; the legal issue must not be masked by the quality issue."""
        d = choose_status(
            make_analysis(people_visible=True, watermark_or_overlay_visible=True),
            make_quality(),
        )
        assert d.status is Status.NEEDS_RELEASE

    def test_watermark_is_low_quality(self):
        d = choose_status(make_analysis(watermark_or_overlay_visible=True), make_quality())
        assert d.status is Status.LOW_QUALITY

    @pytest.mark.parametrize("risk", ["Medium", "High"])
    def test_risky_goes_to_review(self, risk):
        d = choose_status(make_analysis(rejection_risk=risk), make_quality())
        assert d.status is Status.REVIEW

    def test_unknown_risk_goes_to_review(self):
        d = choose_status(make_analysis(rejection_risk="banana"), make_quality())
        assert d.status is Status.REVIEW

    def test_reason_is_never_empty(self):
        for kw in [{}, {"commercial_score": 10}, {"people_visible": True},
                   {"rejection_risk": "High"}, {"watermark_or_overlay_visible": True}]:
            assert choose_status(make_analysis(**kw), make_quality()).reason.strip()

    def test_local_gates_off_by_default(self):
        """Measured softness must not reject anything unless asked."""
        d = choose_status(make_analysis(), make_quality(blur_score=1.0))
        assert d.status is Status.READY

    def test_blur_gate_applies_when_enabled(self):
        d = choose_status(make_analysis(), make_quality(blur_score=1.0), min_blur=100.0)
        assert d.status is Status.LOW_QUALITY

    def test_gates_ignored_when_unmeasured(self):
        unmeasured = make_quality(blur_score=0.0, measured=False)
        d = choose_status(make_analysis(), unmeasured, min_blur=100.0)
        assert d.status is Status.READY

    def test_works_without_quality(self):
        assert choose_status(make_analysis(), None).status is Status.READY
