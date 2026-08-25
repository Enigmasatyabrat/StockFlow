"""Marketplace submission rules.

Every assertion here encodes something checked against official contributor
documentation. If a marketplace changes its spec, these are the tests that
should fail first.
"""

from __future__ import annotations

import pytest

from stockflow import marketplaces as mp
from stockflow.models import Status
from stockflow.rules import target_filename


class TestHeaders:
    def test_shutterstock_header(self):
        """Shutterstock's docs warn that a CSV not formatted exactly as their
        sample "will be rejected"."""
        assert mp.SHUTTERSTOCK_HEADER == ["Filename", "Description", "Keywords", "Categories"]

    def test_adobe_header(self):
        assert mp.ADOBE_HEADER == ["Filename", "Title", "Keywords", "Category", "Releases"]


class TestAdobeCategories:
    def test_there_are_twenty_one(self):
        assert sorted(mp.ADOBE_CATEGORIES) == list(range(1, 22))

    @pytest.mark.parametrize(
        "number,name",
        [(1, "Animals"), (3, "Business"), (7, "Food"), (13, "People"),
         (19, "Technology"), (21, "Travel")],
    )
    def test_known_numbers(self, number, name):
        assert mp.ADOBE_CATEGORIES[number] == name

    def test_every_mapped_target_exists(self):
        for shutterstock in mp._SHUTTERSTOCK_TO_ADOBE:
            number = mp._SHUTTERSTOCK_TO_ADOBE[shutterstock]
            assert number in mp.ADOBE_CATEGORIES

    def test_every_mapping_source_is_a_real_shutterstock_category(self):
        from stockflow.rules import SHUTTERSTOCK_CATEGORIES

        for shutterstock in mp._SHUTTERSTOCK_TO_ADOBE:
            assert shutterstock in SHUTTERSTOCK_CATEGORIES

    def test_returns_a_string_for_csv_use(self):
        assert mp.adobe_category_number("Nature") == "5"

    def test_unmapped_is_blank(self):
        assert mp.adobe_category_number("The Arts") == ""
        assert mp.adobe_category_number("nonsense") == ""


class TestAdobeTitle:
    def test_commas_removed(self):
        assert mp.adobe_title("Desk, laptop, coffee") == "Desk laptop coffee"

    def test_whitespace_collapsed(self):
        assert mp.adobe_title("a,  b") == "a b"

    def test_plain_title_untouched(self):
        assert mp.adobe_title("Sunlit home office") == "Sunlit home office"


class TestWarnings:
    def test_long_filename_warned(self):
        warnings = mp.filename_warnings("a" * 40 + ".jpg")
        assert warnings and "Adobe" in warnings[0]

    def test_short_filename_clean(self):
        assert mp.filename_warnings("sunlit-desk.jpg") == []

    def test_too_few_keywords_warned(self):
        assert mp.keyword_warnings(3)

    def test_enough_keywords_clean(self):
        assert mp.keyword_warnings(40) == []

    def test_overlong_title_warned(self):
        assert mp.title_warnings("x" * 250)

    def test_normal_title_clean(self):
        assert mp.title_warnings("Woman drinking coffee at a laptop") == []


class TestFilenameBudget:
    def test_default_fits_adobes_limit(self):
        """Adobe requires uploaded filenames to be 30 characters or fewer and
        to match the CSV exactly."""
        from pathlib import Path

        name = target_filename(
            "An extremely long descriptive title that would never fit",
            Path("DSC_0001.jpg"), Status.READY, "JPEG",
        )
        assert len(name) <= mp.ADOBE_MAX_FILENAME_CHARS
        assert name.endswith(".jpg")

    def test_budget_accounts_for_the_extension(self):
        from pathlib import Path

        name = target_filename("a" * 100, Path("x.tif"), Status.READY, "TIFF", max_chars=20)
        assert len(name) <= 20
        assert name.endswith(".tif")

    def test_can_be_relaxed(self):
        from pathlib import Path

        name = target_filename(
            "A reasonably long and quite descriptive stock photo title",
            Path("x.jpg"), Status.READY, "JPEG", max_chars=80,
        )
        assert len(name) > 30

    def test_short_title_is_not_padded(self):
        from pathlib import Path

        assert target_filename("Red car", Path("x.jpg"), Status.READY, "JPEG") == "red-car.jpg"
