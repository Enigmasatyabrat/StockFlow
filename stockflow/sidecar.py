"""Analysis supplied as files instead of a model call.

For each image ``PHOTO.jpg`` in the working folder, ``metadata/PHOTO.jpg.json``
holds the same fields the vision model returns (see ``prompt.RESPONSE_SCHEMA``)
and goes through the same validation. Whoever writes those files -- a person, a
script, or an AI assistant working in the folder -- StockFlow then does
everything else exactly as it would for Gemini: dedupe, quality gates, routing,
embedded IPTC/XMP and the marketplace CSVs.

An image with no metadata file yet is simply left pending, so a batch can be
written over several sittings.

``prepare`` writes, for every image still waiting, the preview a vision model
would be sent plus a full-resolution crop of the four corners, where camera
stamps and signatures hide and where a downscaled preview makes them
unreadable.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable

from PIL import Image

from .analyzer import parse_analysis
from .errors import AnalyzerError, MalformedResponseError, MetadataMissing
from .models import Analysis

SIDECAR_DIRNAME = "metadata"
PREVIEW_DIRNAME = "_previews"
PENDING_FILENAME = "_PENDING.txt"

#: Bottom and top strips this tall / wide are cut at full resolution.
CORNER_HEIGHT = 0.12
CORNER_WIDTH = 0.30


def sidecar_dir(folder: Path) -> Path:
    return folder / SIDECAR_DIRNAME


def sidecar_path(folder: Path, source: Path) -> Path:
    """``metadata/<full filename>.json`` -- the extension is kept so
    ``IMG_1.jpg`` and ``IMG_1.png`` can't collide."""
    return sidecar_dir(folder) / f"{source.name}.json"


class SidecarAnalyzer:
    """Reads ``metadata/<file>.json`` for each image. Makes no network calls."""

    def __init__(self, folder: Path):
        self._folder = folder
        self.stats: dict[str, int] = {"calls": 0, "retries": 0, "failures": 0,
                                      "prompt_tokens": 0, "output_tokens": 0}
        self._lock = threading.Lock()

    def analyze(
        self, image_bytes: bytes, quality_note: str = "", *, source: Path | None = None
    ) -> Analysis:
        if source is None:
            raise AnalyzerError("The sidecar provider needs the source file path.")
        path = sidecar_path(self._folder, source)
        if not path.exists():
            raise MetadataMissing(f"no metadata yet ({SIDECAR_DIRNAME}/{path.name})")
        with self._lock:
            self.stats["calls"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise MalformedResponseError(f"{path.name} is not valid JSON: {exc}") from exc
        try:
            return parse_analysis(data)
        except MalformedResponseError as exc:
            raise MalformedResponseError(f"{path.name}: {exc}") from exc


def corner_grid(img: Image.Image, cell: int = 640) -> Image.Image:
    """The four corners, cut from full-resolution pixels, in a 2x2 grid:
    top-left, top-right / bottom-left, bottom-right."""
    w, h = img.size
    cw, ch = max(1, int(w * CORNER_WIDTH)), max(1, int(h * CORNER_HEIGHT))
    boxes = [(0, 0, cw, ch), (w - cw, 0, w, ch), (0, h - ch, cw, h), (w - cw, h - ch, w, h)]
    tiles = []
    for box in boxes:
        tile = img.crop(box)
        tile.thumbnail((cell, cell), Image.LANCZOS)
        tiles.append(tile)
    tw = max(t.width for t in tiles)
    th = max(t.height for t in tiles)
    gap = 8
    grid = Image.new("RGB", (2 * tw + gap, 2 * th + gap), "white")
    for i, tile in enumerate(tiles):
        grid.paste(tile, ((i % 2) * (tw + gap), (i // 2) * (th + gap)))
    return grid


def prepare(folder: Path, sources: Iterable[Path], max_edge: int = 1024) -> list[Path]:
    """Write previews and corner crops for every source without metadata yet.

    Returns the sources that still need a metadata file. Existing sidecars and
    previews are left alone, so re-running is cheap and safe.
    """
    from .imaging import loader

    previews = sidecar_dir(folder) / PREVIEW_DIRNAME
    previews.mkdir(parents=True, exist_ok=True)
    pending: list[Path] = []
    for source in sources:
        if sidecar_path(folder, source).exists():
            continue
        pending.append(source)
        preview = previews / f"{source.name}.preview.jpg"
        corners = previews / f"{source.name}.corners.jpg"
        if preview.exists() and corners.exists():
            continue
        with loader.open_image(source) as img:
            rgb = img.convert("RGB")
            corner_grid(rgb).save(corners, "JPEG", quality=90)
            small = rgb.copy()
            small.thumbnail((max_edge, max_edge), Image.LANCZOS)
            small.save(preview, "JPEG", quality=90)
    (sidecar_dir(folder) / PENDING_FILENAME).write_text(
        "".join(f"{p.name}\n" for p in pending), encoding="utf-8"
    )
    return pending


__all__ = [
    "SIDECAR_DIRNAME", "SidecarAnalyzer", "corner_grid", "prepare", "sidecar_dir", "sidecar_path",
]
