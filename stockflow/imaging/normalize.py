"""Turning a source file into something a marketplace will accept."""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..config import HEIF_IMAGE_TYPES, RAW_IMAGE_TYPES, Settings
from ..errors import ImageDecodeError
from . import loader

log = logging.getLogger(__name__)

#: Formats a marketplace accepts directly. Everything else is converted.
DELIVERABLE_SUFFIXES = {".jpg", ".jpeg", ".tif", ".tiff"}

#: Export quality for derived JPEGs. 4:4:4 chroma (subsampling=0) matters:
#: the default 4:2:0 visibly smears saturated edges, and reviewers do notice.
EXPORT_QUALITY = 95
MIN_EXPORT_QUALITY = 70


@dataclass(frozen=True)
class NormalizeResult:
    path: Path
    derived: bool
    actual_format: str
    converted_from: str = ""
    note: str = ""


def _save_jpeg(img: Image.Image, out: Path, quality: int) -> None:
    img.save(
        out,
        format="JPEG",
        quality=quality,
        subsampling=0,
        optimize=True,
        progressive=True,
        icc_profile=None,  # already converted to sRGB; untagged reads as sRGB
    )


def _size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _unique(directory: Path, name: str) -> Path:
    """Collision-free path. Uses O_EXCL so two workers can't pick the same one."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 0
    while True:
        candidate = directory / (name if n == 0 else f"{stem}-{n}{suffix}")
        try:
            # O_EXCL makes the claim atomic, so two workers racing on the same
            # stem can never both believe they own it.
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return candidate


def plan(path: Path, settings: Settings) -> str:
    """What :func:`normalize` *would* do, without doing it.

    Used by --dry-run so it can describe the conversion without writing a
    derived file to disk.
    """
    suffix = path.suffix.lower()
    if suffix in RAW_IMAGE_TYPES:
        return "RAW would be converted to JPEG"
    if suffix in HEIF_IMAGE_TYPES:
        return "HEIC would be converted to JPEG"
    if _size_mb(path) > settings.max_file_size_mb:
        return f"file over {settings.max_file_size_mb:.0f}MB would be reduced"
    if suffix not in DELIVERABLE_SUFFIXES:
        return f"{suffix.lstrip('.').upper()} would be converted to JPEG"
    try:
        if loader.read_facts(path).orientation != 1:
            return "EXIF orientation would be baked in"
    except Exception:
        pass
    return ""


def normalize(path: Path, settings: Settings) -> NormalizeResult:
    """Produce an upload-ready file, converting only when necessary.

    A JPEG or TIFF that is already within the size limit is returned untouched
    -- re-encoding it would only lose quality. Anything else (PNG, WebP, HEIC,
    RAW, or an oversized file) becomes a derived sRGB JPEG in the work
    directory, leaving the original alone.
    """
    suffix = path.suffix.lower()
    is_raw = suffix in RAW_IMAGE_TYPES
    is_heif = suffix in HEIF_IMAGE_TYPES
    oversize = _size_mb(path) > settings.max_file_size_mb

    needs_convert = is_raw or is_heif or suffix not in DELIVERABLE_SUFFIXES or oversize

    if not needs_convert:
        facts = loader.read_facts(path)
        # A rotated JPEG still needs the orientation baked in, otherwise the
        # delivered file relies on a tag many buyers' software ignores.
        if facts.orientation != 1:
            return _convert(path, settings, reason="EXIF orientation baked in")
        return NormalizeResult(path=path, derived=False, actual_format=facts.fmt)

    reason = (
        "RAW converted to JPEG" if is_raw
        else "HEIC converted to JPEG" if is_heif
        else f"file over {settings.max_file_size_mb:.0f}MB reduced" if oversize
        else f"{suffix.lstrip('.').upper()} converted to JPEG"
    )
    return _convert(path, settings, reason=reason)


def _convert(path: Path, settings: Settings, reason: str) -> NormalizeResult:
    out = _unique(settings.work_dir, f"{path.stem}.jpg")
    try:
        with loader.open_image(path) as img:
            rgb = img if img.mode == "RGB" else img.convert("RGB")
            _save_jpeg(rgb, out, EXPORT_QUALITY)

            quality = EXPORT_QUALITY
            while _size_mb(out) > settings.max_file_size_mb and quality > MIN_EXPORT_QUALITY:
                quality -= 5
                _save_jpeg(rgb, out, quality)

            # Still too big at minimum acceptable quality: scale down instead of
            # degrading further, since resolution loss is less visible than
            # blocking artefacts and marketplaces have generous pixel limits.
            scale = 1.0
            while _size_mb(out) > settings.max_file_size_mb and scale > 0.35:
                scale -= 0.1
                w, h = rgb.size
                resized = rgb.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
                )
                _save_jpeg(resized, out, quality)
                resized.close()
    except ImageDecodeError:
        out.unlink(missing_ok=True)
        raise
    except Exception as exc:
        out.unlink(missing_ok=True)
        raise ImageDecodeError(f"Could not convert {path.name}: {exc}") from exc

    if _size_mb(out) > settings.max_file_size_mb:
        log.warning(
            "%s is still %.1fMB after conversion (limit %.0fMB)",
            out.name, _size_mb(out), settings.max_file_size_mb,
        )

    return NormalizeResult(
        path=out,
        derived=True,
        actual_format="JPEG",
        converted_from=path.suffix.lstrip(".").upper(),
        note=reason,
    )


def encode_for_api(path: Path, max_edge: int = 1024, quality: int = 85) -> bytes:
    """Downsample to a preview the vision model can read.

    Per Gemini's documented tiling, cost depends on aspect ratio rather than
    absolute size once either dimension exceeds 384px, so shrinking below
    1024px would save no tokens while losing detail the model uses for
    composition and subject identification.
    """
    with loader.open_image(path) as img:
        rgb = img if img.mode == "RGB" else img.convert("RGB")
        copy = rgb.copy()
    try:
        copy.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        copy.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    finally:
        copy.close()


def encode_image_for_api(img: Image.Image, max_edge: int = 1024, quality: int = 85) -> bytes:
    """Same as :func:`encode_for_api` for an already-decoded image."""
    copy = img.copy() if img.mode == "RGB" else img.convert("RGB")
    try:
        copy.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        copy.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    finally:
        copy.close()
