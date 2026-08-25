"""Decoding, colour management, orientation and normalisation."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from stockflow.errors import ImageDecodeError, UnsupportedFormatError
from stockflow.imaging import loader, normalize as normalize_mod


class TestFacts:
    def test_reads_dimensions_without_decoding(self, make_image):
        path = make_image("a.jpg", width=1400, height=1100)
        facts = loader.read_facts(path)
        assert (facts.width, facts.height) == (1400, 1100)
        assert facts.megapixels == pytest.approx(1.54, abs=0.01)

    def test_rotated_exif_swaps_reported_axes(self, tmp_path):
        """A phone photo rotated only by an EXIF flag must report its upright
        dimensions, or the resolution gate measures the wrong thing."""
        path = tmp_path / "rot.jpg"
        img = Image.new("RGB", (1200, 800), "red")
        exif = img.getexif()
        exif[0x0112] = 6  # rotate 90 CW
        img.save(path, exif=exif)
        facts = loader.read_facts(path)
        assert (facts.width, facts.height) == (800, 1200)

    def test_unreadable_file_raises(self, tmp_path):
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not an image")
        with pytest.raises(ImageDecodeError):
            loader.read_facts(bad)

    def test_raw_without_rawpy_is_actionable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(loader, "has_raw_support", lambda: False)
        raw = tmp_path / "shot.cr2"
        raw.write_bytes(b"fake raw")
        with pytest.raises(UnsupportedFormatError, match="rawpy"):
            loader.read_facts(raw)


class TestOrientation:
    def test_orientation_is_baked_into_pixels(self, tmp_path):
        path = tmp_path / "rot.jpg"
        img = Image.new("RGB", (1200, 800), "red")
        exif = img.getexif()
        exif[0x0112] = 6
        img.save(path, exif=exif)
        with loader.open_image(path) as opened:
            assert opened.size == (800, 1200)


class TestColourManagement:
    def test_untagged_image_passes_through(self, make_image):
        with loader.open_image(make_image("a.jpg")) as img:
            assert img.mode == "RGB"

    def test_wide_gamut_is_converted_not_reinterpreted(self, tmp_path):
        """A plain .convert('RGB') drops the profile without remapping the
        colours, which makes an AdobeRGB file arrive visibly desaturated."""
        from PIL import ImageCms

        path = tmp_path / "wide.jpg"
        src = Image.new("RGB", (200, 200), (200, 40, 40))
        profile = ImageCms.createProfile("sRGB")  # stand-in for a real wide profile
        src.save(path, icc_profile=ImageCms.ImageCmsProfile(profile).tobytes())

        with loader.open_image(path) as img:
            assert img.mode == "RGB"
            assert "icc_profile" not in img.info, "output must be plain sRGB"


class TestSupport:
    def test_describes_available_codecs(self):
        support = loader.describe_support()
        assert support["jpeg/png/tiff/webp"] is True
        assert isinstance(support["raw"], bool)

    def test_raw_probe_never_raises(self):
        assert isinstance(loader.has_raw_support(), bool)


class TestIterSourceFiles:
    def test_finds_images_and_skips_others(self, settings, make_image):
        make_image("a.jpg", folder=settings.folder)
        (settings.folder / "notes.txt").write_text("hi", encoding="utf-8")
        found = [p.name for p in loader.iter_source_files(settings.folder, settings)]
        assert found == ["a.jpg"]

    def test_skips_dotfiles(self, settings, make_image):
        make_image(".hidden.jpg", folder=settings.folder)
        assert list(loader.iter_source_files(settings.folder, settings)) == []

    def test_is_not_recursive(self, settings, make_image):
        """Output folders are subdirectories of the working folder; recursing
        would re-ingest already-sorted files."""
        make_image("a.jpg", folder=settings.folder / "01_READY_UPLOAD")
        assert list(loader.iter_source_files(settings.folder, settings)) == []


class TestNormalize:
    def test_jpeg_within_limits_is_untouched(self, settings, make_image):
        path = make_image("a.jpg", folder=settings.folder)
        result = normalize_mod.normalize(path, settings)
        assert result.derived is False
        assert result.path == path

    def test_png_is_converted(self, settings, make_image):
        path = make_image("a.png", folder=settings.folder)
        result = normalize_mod.normalize(path, settings)
        assert result.derived is True
        assert result.actual_format == "JPEG"
        assert result.path.suffix == ".jpg"
        assert result.path.parent == settings.work_dir

    def test_conversion_leaves_the_original_alone(self, settings, make_image):
        path = make_image("a.png", folder=settings.folder)
        normalize_mod.normalize(path, settings)
        assert path.exists()

    def test_tiff_within_limits_stays_tiff(self, settings, make_image):
        """This is what makes the .jpg-extension-on-TIFF-bytes bug impossible."""
        path = make_image("a.tif", fmt="TIFF", folder=settings.folder)
        result = normalize_mod.normalize(path, settings)
        assert result.derived is False
        assert result.actual_format == "TIFF"

    def test_oversized_file_is_shrunk(self, settings, make_image):
        path = make_image("big.jpg", folder=settings.folder)
        tiny = settings.with_(max_file_size_mb=0.01)
        result = normalize_mod.normalize(path, tiny)
        assert result.derived is True

    def test_plan_reports_without_writing(self, settings, make_image):
        path = make_image("a.png", folder=settings.folder)
        plan = normalize_mod.plan(path, settings)
        assert "converted" in plan.lower()
        assert not settings.work_dir.exists()


class TestEncodeForApi:
    def test_downsamples(self, settings, make_image):
        path = make_image("a.jpg", width=2000, height=1500, folder=settings.folder)
        data = normalize_mod.encode_for_api(path, max_edge=512)
        import io

        with Image.open(io.BytesIO(data)) as img:
            assert max(img.size) <= 512

    def test_produces_jpeg_bytes(self, settings, make_image):
        data = normalize_mod.encode_for_api(make_image("a.jpg", folder=settings.folder))
        assert data[:2] == b"\xff\xd8"
