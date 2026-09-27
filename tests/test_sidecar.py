"""The sidecar provider: metadata from files, previews, and pipeline behaviour."""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from stockflow import limits as limits_mod
from stockflow.analyzer import FakeAnalyzer
from stockflow.cli import main
from stockflow.config import load_settings
from stockflow.errors import AnalyzerError, MalformedResponseError, MetadataMissing
from stockflow.metadata import ExifToolWriter, FakeMetadataWriter
from stockflow.models import FOLDER_READY, Status
from stockflow.pipeline import Pipeline
from stockflow.sidecar import SidecarAnalyzer, corner_grid, prepare, sidecar_dir, sidecar_path


def write_sidecar(folder, filename, **overrides):
    path = sidecar_path(folder, folder / filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(FakeAnalyzer.sample(**overrides)), encoding="utf-8")
    return path


class TestSidecarAnalyzer:
    def test_reads_and_validates_like_a_model_response(self, tmp_path):
        write_sidecar(tmp_path, "beetle.jpg", title="Red leaf beetle on a dayflower leaf")
        result = SidecarAnalyzer(tmp_path).analyze(b"", source=tmp_path / "beetle.jpg")
        assert result.title == "Red leaf beetle on a dayflower leaf"
        assert result.keywords

    def test_missing_file_means_not_written_yet(self, tmp_path):
        with pytest.raises(MetadataMissing, match="beetle.jpg.json"):
            SidecarAnalyzer(tmp_path).analyze(b"", source=tmp_path / "beetle.jpg")

    def test_invalid_json_names_the_file(self, tmp_path):
        path = sidecar_path(tmp_path, tmp_path / "beetle.jpg")
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(MalformedResponseError, match="beetle.jpg.json"):
            SidecarAnalyzer(tmp_path).analyze(b"", source=tmp_path / "beetle.jpg")

    def test_invalid_content_names_the_file(self, tmp_path):
        write_sidecar(tmp_path, "beetle.jpg", title="")
        with pytest.raises(MalformedResponseError, match="beetle.jpg.json.*no title"):
            SidecarAnalyzer(tmp_path).analyze(b"", source=tmp_path / "beetle.jpg")

    def test_tolerates_a_utf8_bom(self, tmp_path):
        # Windows Notepad saves "UTF-8" with a byte-order mark.
        path = sidecar_path(tmp_path, tmp_path / "a.jpg")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(FakeAnalyzer.sample()), encoding="utf-8-sig")
        assert SidecarAnalyzer(tmp_path).analyze(b"", source=tmp_path / "a.jpg").title

    def test_needs_the_source_path(self, tmp_path):
        with pytest.raises(AnalyzerError):
            SidecarAnalyzer(tmp_path).analyze(b"")

    def test_keeps_the_extension_so_names_cannot_collide(self, tmp_path):
        assert sidecar_path(tmp_path, tmp_path / "IMG_1.jpg") != sidecar_path(tmp_path, tmp_path / "IMG_1.png")


class TestCornerGrid:
    def test_small_corner_text_survives_that_a_preview_would_lose(self):
        # A 6x6 px red stamp in the bottom-right of a 4000x3000 photo: about 1.5 px
        # in a 1024 px preview, but clearly present in the corner crop.
        arr = np.full((3000, 4000, 3), 200, dtype=np.uint8)
        arr[2980:2986, 3980:3986] = (255, 0, 0)
        img = Image.fromarray(arr)
        grid = np.asarray(corner_grid(img))
        h, w = grid.shape[:2]
        bottom_right = grid[h // 2:, w // 2:]
        red = (bottom_right[..., 0] > 220) & (bottom_right[..., 1] < 80)
        assert red.sum() >= 4

    def test_quadrants_hold_the_matching_corners(self):
        arr = np.zeros((1000, 1000, 3), dtype=np.uint8)
        arr[:100, :100] = (255, 0, 0)      # top-left red
        arr[-100:, -100:] = (0, 0, 255)    # bottom-right blue
        grid = np.asarray(corner_grid(Image.fromarray(arr)))
        assert grid[5, 5, 0] > 200
        assert grid[-5, -5, 2] > 200


class TestPrepare:
    def test_writes_previews_and_lists_only_pending_images(self, tmp_path, make_image):
        a = make_image("a.jpg", folder=tmp_path)
        b = make_image("b.jpg", folder=tmp_path)
        write_sidecar(tmp_path, "b.jpg")

        pending = prepare(tmp_path, [a, b])

        assert pending == [a]
        previews = sidecar_dir(tmp_path) / "_previews"
        assert (previews / "a.jpg.preview.jpg").exists()
        assert (previews / "a.jpg.corners.jpg").exists()
        assert not (previews / "b.jpg.preview.jpg").exists()
        assert (sidecar_dir(tmp_path) / "_PENDING.txt").read_text(encoding="utf-8") == "a.jpg\n"
        with Image.open(previews / "a.jpg.preview.jpg") as im:
            assert max(im.size) <= 1024

    def test_is_safe_to_rerun(self, tmp_path, make_image):
        a = make_image("a.jpg", folder=tmp_path)
        assert prepare(tmp_path, [a]) == [a]
        assert prepare(tmp_path, [a]) == [a]


class TestPipeline:
    def test_images_with_metadata_are_processed_and_the_rest_stay_pending(self, settings, make_image):
        make_image("done.jpg", folder=settings.folder)
        make_image("later.jpg", folder=settings.folder)
        write_sidecar(settings.folder, "done.jpg", title="Chandelier with vintage glass shades")

        s = settings.with_(provider="sidecar")
        pipeline = Pipeline(s, SidecarAnalyzer(s.folder), FakeMetadataWriter())
        result = pipeline.run()

        assert pipeline.registry.status_of("done.jpg") is Status.READY
        assert any((s.folder / FOLDER_READY).iterdir())
        # Not written yet: untouched, not an error, and it didn't stop the run.
        assert (s.folder / "later.jpg").exists()
        assert pipeline.registry.status_of("later.jpg") is not Status.ERROR
        assert pipeline.registry.is_pending("later.jpg", max_attempts=3)
        assert result.summary["remaining"] == 1

    def test_no_quota_applies(self):
        assert limits_mod.for_provider("sidecar", "anything") is limits_mod.LOCAL

    def test_provider_is_accepted_by_settings(self, photo_folder):
        s = load_settings({"folder": str(photo_folder), "provider": "sidecar"}, env={})
        assert s.provider == "sidecar"


class TestCli:
    @pytest.fixture(autouse=True)
    def exiftool_present(self, monkeypatch):
        monkeypatch.setattr(ExifToolWriter, "available", lambda self: True)

    def test_missing_metadata_folder_is_explained(self, photo_folder, capsys):
        code = main([str(photo_folder), "--provider", "sidecar"])
        assert code == 4
        assert "--prepare-sidecars" in capsys.readouterr().err

    def test_prepare_sidecars_writes_the_worklist_and_moves_nothing(self, photo_folder, make_image, capsys):
        make_image("x.jpg", folder=photo_folder)
        before = sorted(p.name for p in photo_folder.iterdir())
        code = main([str(photo_folder), "--prepare-sidecars", "--min-megapixels", "1"])
        out = capsys.readouterr().out
        assert code == 0
        assert "1 pending" in out
        assert (photo_folder / "metadata" / "_PENDING.txt").read_text(encoding="utf-8") == "x.jpg\n"
        assert sorted(p.name for p in photo_folder.iterdir()) == sorted(before + ["metadata"])
