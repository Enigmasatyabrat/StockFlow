"""Decoding, colour management and orientation.

Everything that turns "a file on disk" into "correct sRGB pixels the right way
up" lives here. Nothing in this module knows what a *good* photo is.
"""

from __future__ import annotations

import io
import logging
import os
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageCms, ImageFile, ImageOps

from ..config import HEIF_IMAGE_TYPES, RAW_IMAGE_TYPES, Settings
from ..errors import ImageDecodeError, UnsupportedFormatError
from ..models import ImageFacts

log = logging.getLogger(__name__)

# Legitimate ultra-high-resolution stock work exceeds Pillow's default guard.
# 500MP is ~1.5GB as 8-bit RGB, which bounds a single decode.
Image.MAX_IMAGE_PIXELS = 500_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = True
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

_codecs_registered = False
_SRGB_PROFILE = None


def register_codecs() -> None:
    """Register optional Pillow plugins. Idempotent and safe to call anywhere."""
    global _codecs_registered
    if _codecs_registered:
        return
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception as exc:  # pragma: no cover - depends on optional install
        log.debug("HEIF support unavailable: %s", exc)
    _codecs_registered = True


def has_heif_support() -> bool:
    register_codecs()
    try:
        import pillow_heif  # noqa: F401

        return True
    except Exception:
        return False


def has_raw_support() -> bool:
    """True when rawpy is importable. Never raises."""
    try:
        import rawpy  # noqa: F401

        return True
    except Exception:
        return False


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_IMAGE_TYPES


def _srgb_profile():
    global _SRGB_PROFILE
    if _SRGB_PROFILE is None:
        _SRGB_PROFILE = ImageCms.createProfile("sRGB")
    return _SRGB_PROFILE


def to_srgb(img: Image.Image) -> Image.Image:
    """Convert to sRGB, honouring any embedded ICC profile.

    Marketplaces expect sRGB. A wide-gamut file (Adobe RGB from a DSLR,
    Display P3 from an iPhone, ProPhoto from Lightroom) that is merely
    reinterpreted as sRGB comes out visibly desaturated and flat -- a common
    and entirely avoidable rejection.

    A plain ``.convert("RGB")`` does exactly that reinterpretation: it drops
    the profile without remapping the colours. This does the real transform,
    and falls back to a plain convert only when the profile is unusable.
    """
    icc = img.info.get("icc_profile")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            converted = ImageCms.profileToProfile(
                img, src, _srgb_profile(), outputMode="RGB"
            )
            if converted is not None:
                converted.info.pop("icc_profile", None)
                return converted
        except Exception as exc:
            log.debug("ICC conversion failed (%s); falling back to direct convert", exc)

    # No profile: untagged data is sRGB by convention.
    return img.convert("RGB") if img.mode != "RGB" else img


def apply_orientation(img: Image.Image) -> Image.Image:
    """Bake the EXIF orientation flag into the pixels.

    Phone and mirrorless cameras record rotation as a tag rather than rotating
    the sensor data. Without this the model is shown a sideways image -- so it
    describes a sideways image -- and the delivered file is sideways too.
    ``exif_transpose`` also clears the tag so it can't be applied twice.
    """
    try:
        return ImageOps.exif_transpose(img) or img
    except Exception as exc:
        log.debug("EXIF orientation could not be applied: %s", exc)
        return img


def _raw_facts(path: Path) -> ImageFacts:
    import rawpy

    with rawpy.imread(str(path)) as raw:
        sizes = raw.sizes
        # `width`/`height` are the postprocessed output dimensions, already
        # corrected for orientation. `raw_width` includes masked border pixels
        # that never reach the final image, so gating megapixels on it would
        # over-count and let genuinely small RAWs through.
        return ImageFacts(
            width=int(sizes.width),
            height=int(sizes.height),
            fmt="RAW",
            mode="RGB",
            is_raw=True,
            orientation=1,
        )


def read_facts(path: Path) -> ImageFacts:
    """Dimensions and format without decoding pixels."""
    if is_raw(path):
        if not has_raw_support():
            raise UnsupportedFormatError(
                f"{path.name} is a RAW file but the 'rawpy' package is not installed. "
                f"Install it with:  pip install stockflow[raw]"
            )
        try:
            return _raw_facts(path)
        except Exception as exc:
            raise ImageDecodeError(f"Could not read RAW header of {path.name}: {exc}") from exc

    register_codecs()
    try:
        with Image.open(path) as img:
            orientation = 1
            try:
                exif = img.getexif()
                orientation = int(exif.get(0x0112, 1) or 1)
            except Exception:
                pass
            width, height = img.size
            # Orientation 5-8 are the 90-degree rotations, which swap the axes.
            if orientation in (5, 6, 7, 8):
                width, height = height, width
            return ImageFacts(
                width=width,
                height=height,
                fmt=(img.format or path.suffix.lstrip(".").upper()),
                mode=img.mode,
                is_raw=False,
                orientation=orientation,
            )
    except UnsupportedFormatError:
        raise
    except Exception as exc:
        raise ImageDecodeError(f"Could not read {path.name}: {exc}") from exc


def _decode_raw(path: Path) -> Image.Image:
    """Full RAW demosaic, tuned for faithful reproduction rather than punch.

    Stock reviewers reject over-processed submissions, so this deliberately
    avoids anything that would look like an edit: camera white balance (what
    the photographer metered), no auto-brightening, and clipped highlights
    rather than a reconstruction that invents colour.
    """
    import numpy as np
    import rawpy

    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(
            use_camera_wb=True,
            output_color=rawpy.ColorSpace.sRGB,
            output_bps=8,
            no_auto_bright=True,
            demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
            highlight_mode=rawpy.HighlightMode.Clip,
            gamma=(2.222, 4.5),
        )
    return Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")


def extract_raw_preview(path: Path) -> Image.Image | None:
    """Pull the camera's embedded JPEG preview out of a RAW file.

    Orders of magnitude faster than a full demosaic. Good enough for the model
    thumbnail and the perceptual hash; deliberately NOT used for the quality
    metrics, because the preview is the camera's own JPEG rendering and its
    sharpening and noise reduction would make the measurements describe the
    preview rather than the capture.
    """
    try:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data))
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                return Image.fromarray(thumb.data)
    except Exception as exc:
        log.debug("No usable embedded preview in %s: %s", path.name, exc)
    return None


@contextmanager
def open_image(path: Path, *, srgb: bool = True, oriented: bool = True) -> Iterator[Image.Image]:
    """Open any supported file as upright sRGB pixels.

    Always use this instead of ``Image.open``: it closes the underlying file
    handle. On Windows a lingering handle blocks the later ``shutil.move`` and
    the file appears mysteriously locked.
    """
    register_codecs()
    suffix = path.suffix.lower()

    if suffix in RAW_IMAGE_TYPES:
        if not has_raw_support():
            raise UnsupportedFormatError(
                f"{path.name} is a RAW file but 'rawpy' is not installed. "
                f"Install it with:  pip install stockflow[raw]"
            )
        try:
            img = _decode_raw(path)
        except Exception as exc:
            raise ImageDecodeError(f"Could not decode RAW {path.name}: {exc}") from exc
        try:
            yield img  # rawpy output is already sRGB and upright
        finally:
            img.close()
        return

    if suffix in HEIF_IMAGE_TYPES and not has_heif_support():
        raise UnsupportedFormatError(
            f"{path.name} is a HEIC/HEIF file but 'pillow-heif' is not installed."
        )

    handle = None
    result = None
    try:
        handle = Image.open(path)
        handle.load()
        result = handle
        if oriented:
            result = apply_orientation(result)
        if srgb:
            result = to_srgb(result)
        yield result
    except (UnsupportedFormatError, ImageDecodeError):
        raise
    except Exception as exc:
        raise ImageDecodeError(f"Could not decode {path.name}: {exc}") from exc
    finally:
        for obj in {id(result): result, id(handle): handle}.values():
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass


def iter_source_files(folder: Path, settings: Settings) -> Iterator[Path]:
    """Yield candidate images sitting directly in ``folder``.

    Deliberately non-recursive: the output folders are subdirectories of the
    working folder, and recursing would re-ingest already-sorted files.
    """
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
    except OSError as exc:
        raise ImageDecodeError(f"Could not list {folder}: {exc}") from exc

    for path in entries:
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() in settings.image_types:
            yield path


def describe_support() -> dict[str, bool]:
    """What this install can actually decode. Shown in the startup banner."""
    return {
        "jpeg/png/tiff/webp": True,
        "heic/heif": has_heif_support(),
        "raw": has_raw_support(),
    }
