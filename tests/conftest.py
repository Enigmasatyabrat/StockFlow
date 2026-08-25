"""Shared fixtures. No network, no API key, no real exiftool required."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageFilter

from stockflow.analyzer import FakeAnalyzer
from stockflow.config import Settings, load_settings
from stockflow.metadata import FakeMetadataWriter
from stockflow.pipeline import Pipeline


def checkerboard(width: int, height: int, cell: int = 14, seed: int = 0) -> Image.Image:
    """A high-frequency pattern -- cheap to generate and reliably 'sharp'.

    ``seed`` shifts the phase so two images requested under different names
    are not byte-identical. Without it every generated image collides as an
    exact duplicate and tests silently measure the wrong thing.
    """
    yy, xx = np.mgrid[0:height, 0:width]
    plane = (((yy // cell + xx // cell) % 2) * 190 + 35).astype(np.uint8)
    if seed:
        plane = np.roll(plane, seed % max(1, cell), axis=1)
        plane[: min(4, height), : min(4, width)] = (seed * 37) % 256
    return Image.fromarray(np.dstack([plane] * 3))


@pytest.fixture
def make_image(tmp_path):
    """Write a generated test image and return its path.

    Generating beats committing binaries: the suite stays small, and each test
    can ask for exactly the pathology it needs.
    """

    def _make(
        name: str = "photo.jpg",
        width: int = 1400,
        height: int = 1100,
        *,
        blur: float = 0.0,
        noise: float = 0.0,
        brightness: int = 0,
        fmt: str | None = None,
        folder=None,
        seed: int | None = None,
    ):
        # Derive a per-name seed so distinct filenames yield distinct bytes.
        img = checkerboard(width, height, seed=sum(name.encode()) if seed is None else seed)
        if blur:
            img = img.filter(ImageFilter.GaussianBlur(blur))
        if noise:
            rng = np.random.default_rng(0)
            arr = np.asarray(img, dtype=np.int16) + rng.normal(0, noise, (height, width, 3))
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if brightness:
            arr = np.asarray(img, dtype=np.int16) + brightness
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

        target = (folder or tmp_path) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        save_fmt = fmt or ("PNG" if target.suffix.lower() == ".png" else "JPEG")
        if save_fmt == "JPEG":
            img.save(target, format="JPEG", quality=95)
        else:
            img.save(target, format=save_fmt)
        return target

    return _make


@pytest.fixture
def photo_folder(tmp_path):
    folder = tmp_path / "batch"
    folder.mkdir()
    return folder


@pytest.fixture
def settings(photo_folder) -> Settings:
    # A low resolution floor keeps the generated fixtures small and the suite
    # fast; tests that care about the resolution gate set their own images
    # well below it.
    return load_settings(
        {
            "folder": str(photo_folder),
            "workers": 1,
            "api_key": "test-key",
            "min_megapixels": 1.0,
        },
        env={},
    )


@pytest.fixture
def fake_analyzer() -> FakeAnalyzer:
    return FakeAnalyzer()


@pytest.fixture
def fake_writer() -> FakeMetadataWriter:
    return FakeMetadataWriter()


@pytest.fixture
def make_pipeline(settings, fake_analyzer, fake_writer):
    def _make(**overrides):
        s = settings.with_(**overrides) if overrides else settings
        analyzer = overrides.pop("analyzer", None) or fake_analyzer
        return Pipeline(s, analyzer, fake_writer)

    return _make
