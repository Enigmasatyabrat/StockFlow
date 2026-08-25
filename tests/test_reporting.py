"""CSV contracts, reports and the review log."""

from __future__ import annotations

import csv
import json
import threading

import pytest

from stockflow.models import ItemRecord, Status
from stockflow.reporting import (
    ADOBE_HEADER,
    SHUTTERSTOCK_HEADER,
    ReviewLog,
    UploadCsv,
    adobe_row,
    shutterstock_row,
    write_report,
)


def make_record(**kw) -> ItemRecord:
    base = dict(
        original_name="a.jpg",
        final_name="sunlit-desk.jpg",
        status=Status.READY,
        title="Sunlit desk with laptop",
        category="Business/Finance",
        category2="Interiors",
    )
    base.update(kw)
    return ItemRecord(**base)


class TestShutterstockCsv:
    def test_header_is_exact(self):
        """An external contract -- changing it silently corrupts every upload."""
        assert SHUTTERSTOCK_HEADER == ["Filename", "Description", "Keywords", "Categories"]

    def test_title_goes_into_the_description_column(self):
        """Deliberately counter-intuitive: Shutterstock's Description field is
        the searchable headline shown to buyers, not a caption."""
        row = shutterstock_row(make_record(), ["sky", "blue"])
        assert row[1] == "Sunlit desk with laptop"

    def test_keywords_are_comma_joined(self):
        assert shutterstock_row(make_record(), ["sky", "blue"])[2] == "sky, blue"

    def test_both_categories_joined(self):
        assert shutterstock_row(make_record(), [])[3] == "Business/Finance, Interiors"

    def test_single_category_has_no_trailing_separator(self):
        assert shutterstock_row(make_record(category2=""), [])[3] == "Business/Finance"


class TestAdobeCsv:
    def test_header_matches_adobes_template(self):
        """Verified byte-for-byte against Adobe's own downloadable sample at
        contributor.stock.adobe.com/static/csv/Sample_Adobe_Stock_CSV_upload.csv"""
        assert ADOBE_HEADER == ["Filename", "Title", "Keywords", "Category", "Releases"]

    def test_category_is_a_number_not_a_name(self):
        """Adobe's Category column takes a number -- their sample row uses `3`.
        Writing a Shutterstock category name like 'Business/Finance' there is
        simply invalid."""
        row = adobe_row(make_record(category="Business/Finance"), [])
        assert row[3] == "3"

    @pytest.mark.parametrize(
        "shutterstock,adobe",
        [("Nature", "5"), ("People", "13"), ("Technology", "19"),
         ("Transportation", "20"), ("Animals/Wildlife", "1")],
    )
    def test_category_mapping(self, shutterstock, adobe):
        assert adobe_row(make_record(category=shutterstock), [])[3] == adobe

    @pytest.mark.parametrize("unmappable", ["The Arts", "Education", "Miscellaneous", "Vintage"])
    def test_unmappable_category_is_left_blank(self, unmappable):
        """Adobe treats Category as optional and auto-suggests one, so a blank
        beats forcing the image into a wrong bucket."""
        assert adobe_row(make_record(category=unmappable), [])[3] == ""

    def test_releases_is_always_blank(self):
        """The Releases column takes the filenames of release documents already
        uploaded to the contributor portal, not a flag. StockFlow cannot know
        those names, so it must not invent a value."""
        assert adobe_row(make_record(people_visible=True), [])[4] == ""
        assert adobe_row(make_record(), [])[4] == ""

    def test_commas_stripped_from_title(self):
        """Adobe documents that the Title must contain no commas."""
        row = adobe_row(make_record(title="Desk, laptop, and coffee"), [])
        assert "," not in row[1]
        assert row[1] == "Desk laptop and coffee"


class TestUploadCsv:
    def test_writes_header_once(self, tmp_path):
        csv_file = UploadCsv(tmp_path / "out.csv", SHUTTERSTOCK_HEADER)
        csv_file.append(["a.jpg", "T", "k", "C"])
        csv_file.append(["b.jpg", "T", "k", "C"])
        lines = (tmp_path / "out.csv").read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == "Filename,Description,Keywords,Categories"
        assert len(lines) == 3

    def test_unicode_round_trips(self, tmp_path):
        csv_file = UploadCsv(tmp_path / "out.csv", SHUTTERSTOCK_HEADER)
        csv_file.append(["café-日本.jpg", "Café — 日本", "café, 桜", "Nature"])
        rows = list(csv.reader((tmp_path / "out.csv").open(encoding="utf-8")))
        assert rows[1][0] == "café-日本.jpg"

    def test_commas_in_values_are_quoted(self, tmp_path):
        csv_file = UploadCsv(tmp_path / "out.csv", SHUTTERSTOCK_HEADER)
        csv_file.append(["a.jpg", "Title, with comma", "k1, k2", "C"])
        rows = list(csv.reader((tmp_path / "out.csv").open(encoding="utf-8")))
        assert rows[1][1] == "Title, with comma"
        assert rows[1][2] == "k1, k2"

    def test_concurrent_appends_lose_nothing(self, tmp_path):
        csv_file = UploadCsv(tmp_path / "out.csv", SHUTTERSTOCK_HEADER)

        def worker(n):
            for i in range(20):
                csv_file.append([f"{n}-{i}.jpg", "T", "k", "C"])

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = list(csv.reader((tmp_path / "out.csv").open(encoding="utf-8")))
        assert len(rows) == 81  # header + 4*20
        assert rows.count(SHUTTERSTOCK_HEADER) == 1


class TestReviewLog:
    def test_appends(self, tmp_path):
        log = ReviewLog(tmp_path / "review.txt")
        log.write("first")
        log.write("second")
        assert (tmp_path / "review.txt").read_text(encoding="utf-8").splitlines() == [
            "first", "second",
        ]

    def test_unicode_is_safe(self, tmp_path):
        log = ReviewLog(tmp_path / "review.txt")
        log.write("日本の桜 café")
        assert "日本の桜" in (tmp_path / "review.txt").read_text(encoding="utf-8")


class TestWriteReport:
    def test_writes_json_and_csv(self, tmp_path):
        paths = write_report(tmp_path, [make_record().as_dict()], {"ready_to_upload": 1})
        assert paths["latest_json"].exists() and paths["latest_csv"].exists()
        assert paths["run_json"].exists() and paths["run_csv"].exists()

    def test_run_reports_accumulate(self, tmp_path):
        """v4 truncated report.json every run, destroying the audit trail."""
        first = write_report(tmp_path, [], {}, stamp="20260101T000000Z")
        second = write_report(tmp_path, [], {}, stamp="20260102T000000Z")
        assert first["run_json"].exists() and second["run_json"].exists()

    def test_same_second_runs_do_not_collide(self, tmp_path):
        first = write_report(tmp_path, [], {}, stamp="20260101T000000Z")
        second = write_report(tmp_path, [], {}, stamp="20260101T000000Z")
        assert first["run_json"] != second["run_json"]
        assert first["run_json"].exists() and second["run_json"].exists()

    def test_json_payload_shape(self, tmp_path):
        paths = write_report(tmp_path, [make_record().as_dict()], {"ready_to_upload": 1})
        payload = json.loads(paths["latest_json"].read_text(encoding="utf-8"))
        assert payload["summary"]["ready_to_upload"] == 1
        assert payload["items"][0]["original_name"] == "a.jpg"

    def test_csv_includes_quality_columns(self, tmp_path):
        record = make_record()
        record.quality = {"blur": 412.5, "noise": 3.1, "clip_low": 0.0,
                          "clip_high": 0.01, "flags": ["soft"]}
        paths = write_report(tmp_path, [record.as_dict()], {})
        rows = list(csv.reader(paths["latest_csv"].open(encoding="utf-8")))
        assert "blur" in rows[0]
        assert "412.5" in rows[1]
        assert "soft" in rows[1]
