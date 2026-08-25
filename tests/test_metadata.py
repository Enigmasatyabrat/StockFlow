"""Metadata embedding.

The tests marked ``integration`` drive the real exiftool binary, because the
bugs they guard against are properties of exiftool's argument handling and
cannot be reproduced against a fake. They skip automatically when the binary
is unavailable.
"""

from __future__ import annotations

import pytest
from PIL import Image

from stockflow.config import find_exiftool
from stockflow.errors import MetadataWriteError
from stockflow.metadata import ExifToolWriter, FakeMetadataWriter, _build_args

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def writer():
    w = ExifToolWriter(find_exiftool())
    if not w.available():
        pytest.skip("exiftool binary not available")
    return w


@pytest.fixture
def jpeg(tmp_path):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 64), "red").save(path)
    return path


class TestArgumentConstruction:
    def test_keyword_tags_are_cleared_before_writing(self):
        """`-Keywords+=` appends. Verified against exiftool 13.52: running it
        twice over one file yields 'alpha, beta, alpha, gamma'."""
        args = _build_args_for(["sky"])
        assert "-IPTC:Keywords=" in args
        assert "-XMP-dc:Subject=" in args

    def test_uses_plain_assignment_not_append(self):
        args = _build_args_for(["sky"])
        assert not any(a.startswith("-IPTC:Keywords+=") for a in args)
        assert "-IPTC:Keywords=sky" in args

    def test_clear_comes_before_the_values(self):
        args = _build_args_for(["sky", "blue"])
        assert args.index("-IPTC:Keywords=") < args.index("-IPTC:Keywords=sky")

    def test_writes_both_iptc_and_xmp(self):
        args = _build_args_for(["sky"])
        assert "-IPTC:Keywords=sky" in args and "-XMP-dc:Subject=sky" in args

    def test_path_is_last(self, tmp_path):
        args = _build_args(tmp_path / "x.jpg", "T", "D", ["k"], {})
        assert args[-1] == str(tmp_path / "x.jpg")


def _build_args_for(keywords):
    from pathlib import Path

    return _build_args(Path("x.jpg"), "Title", "Desc", keywords, {})


@pytest.mark.integration
class TestRealExifTool:
    def test_round_trips_ascii(self, writer, jpeg):
        writer.write(jpeg, title="A Title", description="A description.",
                     keywords=["sky", "blue"])
        tags = writer.read_tags(jpeg, ["IPTC:Keywords", "XMP-dc:Title"])
        assert tags["Keywords"] == ["sky", "blue"]
        assert tags["Title"] == "A Title"

    def test_is_idempotent(self, writer, jpeg):
        """The retry path re-embeds metadata on a file that already has it, so
        a non-idempotent write silently doubles every keyword."""
        for _ in range(3):
            writer.write(jpeg, title="T", description="D", keywords=["sky", "blue"])
        assert writer.read_tags(jpeg, ["IPTC:Keywords"])["Keywords"] == ["sky", "blue"]

    def test_replaces_pre_existing_keywords(self, writer, jpeg):
        """A photographer's JPEGs often already carry Lightroom keywords;
        appending would import private tags into a commercial submission."""
        writer.write(jpeg, title="T", description="D", keywords=["old1", "old2"])
        writer.write(jpeg, title="T", description="D", keywords=["new1"])
        assert writer.read_tags(jpeg, ["IPTC:Keywords"])["Keywords"] == ["new1"]

    def test_unicode_survives(self, writer, jpeg):
        """Passed as command-line arguments these arrive mangled on Windows --
        'café' became 'caf?' and '日本の桜' became '????' -- with exiftool
        still exiting 0, so nothing detected it."""
        writer.write(
            jpeg,
            title="Café à Paris — 日本",
            description="Une description en français.",
            keywords=["café", "日本の桜", "naïve"],
        )
        tags = writer.read_tags(jpeg, ["XMP-dc:Title", "IPTC:Keywords", "IPTC:Caption-Abstract"])
        assert tags["Title"] == "Café à Paris — 日本"
        assert tags["Keywords"] == ["café", "日本の桜", "naïve"]
        assert "français" in tags["Caption-Abstract"]

    def test_unicode_filename_is_handled(self, writer, tmp_path):
        """The file path has to go through the argfile too: passed as an
        argument, exiftool reports 'Invalid filename encoding / No matching
        files'."""
        path = tmp_path / "日本の桜-café.jpg"
        Image.new("RGB", (32, 32), "blue").save(path)
        writer.write(path, title="Sakura", description="D", keywords=["sakura"])
        assert writer.read_tags(path, ["XMP-dc:Title"])["Title"] == "Sakura"

    def test_many_keywords_exceed_no_command_line_limit(self, writer, jpeg):
        """50 keywords across two tag families approaches the Windows 32KB
        command-line ceiling."""
        keywords = [f"keyword-number-{i:03d}" for i in range(50)]
        writer.write(jpeg, title="T", description="D", keywords=keywords)
        assert len(writer.read_tags(jpeg, ["IPTC:Keywords"])["Keywords"]) == 50

    def test_title_starting_with_dash_is_not_an_option(self, writer, jpeg):
        """A model-authored title beginning with '-' would otherwise be read
        as an exiftool flag."""
        writer.write(jpeg, title="-overwrite_original_in_place", description="D",
                     keywords=["k"])
        assert writer.read_tags(jpeg, ["XMP-dc:Title"])["Title"] == "-overwrite_original_in_place"

    def test_copied_metadata_marks_orientation_normal(self, writer, tmp_path):
        """Rotation is baked into the derived pixels, so the tag must read
        'normal'. Plain `-Orientation=1` goes through exiftool's PrintConv and
        silently writes 3 ('Rotate 180'), making viewers re-rotate a correct
        image. Verified against exiftool 13.52."""
        source = tmp_path / "src.jpg"
        img = Image.new("RGB", (100, 60), "red")
        exif = img.getexif()
        exif[0x0112] = 6
        img.save(source, exif=exif)

        target = tmp_path / "derived.jpg"
        Image.new("RGB", (60, 100), "blue").save(target)
        writer.copy_capture_metadata(source, target)

        with Image.open(target) as out:
            assert out.getexif().get(0x0112, 1) == 1

    def test_missing_file_raises(self, writer, tmp_path):
        with pytest.raises(MetadataWriteError):
            writer.write(tmp_path / "nope.jpg", title="T", description="D", keywords=["k"])

    def test_missing_binary_gives_actionable_error(self, tmp_path, jpeg):
        broken = ExifToolWriter("definitely-not-a-real-binary")
        with pytest.raises(MetadataWriteError, match="not found"):
            broken.write(jpeg, title="T", description="D", keywords=["k"])


class TestFakeWriter:
    def test_records_calls(self, tmp_path):
        fake = FakeMetadataWriter()
        fake.write(tmp_path / "x.jpg", title="T", description="D", keywords=["a"])
        assert fake.written[0]["title"] == "T"

    def test_can_simulate_failure(self, tmp_path):
        fake = FakeMetadataWriter(fail_on={"x.jpg"})
        with pytest.raises(MetadataWriteError):
            fake.write(tmp_path / "x.jpg", title="T", description="D", keywords=["a"])
