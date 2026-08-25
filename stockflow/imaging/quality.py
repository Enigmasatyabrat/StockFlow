"""Local, measured image quality -- numpy only, no OpenCV, no scipy.

WHY THIS EXISTS
---------------
v4 downsampled every photo to 1024px at JPEG quality 85 and then asked the
vision model to score "technical quality (sharpness, exposure, noise) -- up to
30 points". Sharpness and sensor noise are physically destroyed by that
resample. The model was being asked to grade information that no longer
existed, and technical faults are what marketplaces actually reject for.

So the numbers here are measured on the real pixels, and the model is told what
they are instead of guessing.

THE SCALE PROBLEM
-----------------
Variance-of-Laplacian -- the standard blur metric -- is strongly scale
dependent. Measured on the same photograph, full-resolution 24MP output was
~53x the value obtained at 1024px. Any fixed threshold ("blurry if < 100",
as widely quoted) is therefore meaningless unless the measurement scale is
pinned down.

The fix is to sample fixed-size TILES at NATIVE resolution. A 256x256 tile of a
50MP file and a 256x256 tile of a 12MP file are directly comparable, because
each covers the same number of real sensor pixels. It also makes cost constant:
at most GRID*GRID*TILE*TILE pixels are examined no matter how large the file.

THE BOKEH PROBLEM
-----------------
A sharp subject against a soft background is *good* stock, not a reject. Mean
sharpness across the frame would punish exactly the shallow-depth-of-field work
that sells best. So the reported focus score is a high percentile across tiles:
it answers "is anything in this frame properly sharp?", not "is all of it
sharp?".

CALIBRATION HONESTY
-------------------
The thresholds in `DEFAULT_*` are starting points derived from the maths and
from typical values, not from a labelled dataset. They are deliberately
permissive, and gating on them is opt-in (`--min-blur` etc.). Everything is
measured and reported always; nothing is rejected on these numbers unless the
user asks for it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image

from ..models import QualityReport

log = logging.getLogger(__name__)

#: Edge length of each sampled tile, in native pixels.
TILE = 256
#: Tiles are sampled on at most GRID x GRID positions.
GRID = 8
#: Percentile of per-tile sharpness used as the focus score. 90th = "the
#: sharpest tenth of the frame", which is what a shallow-DOF subject occupies.
FOCUS_PERCENTILE = 90
#: Exposure/contrast are scale-invariant, so they run on a cheap thumbnail.
HISTOGRAM_EDGE = 1024

# --- Starting-point thresholds. Tune to your own portfolio. -------------------
DEFAULT_MIN_BLUR = 120.0      # below this, nothing in frame is convincingly sharp
DEFAULT_MAX_NOISE = 6.0       # 8-bit sigma in flat regions
DEFAULT_MAX_CLIPPING = 0.05   # 5% of pixels at pure black or pure white
_CLIP_DARK = 2
_CLIP_BRIGHT = 253

# Rec. 709 luma weights.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _to_luma(img: Image.Image) -> np.ndarray:
    """Float32 luma plane in 0..255."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.asarray(img, dtype=np.uint8)
    if arr.ndim == 2:
        return arr.astype(np.float32)
    return arr[:, :, :3].astype(np.float32) @ _LUMA


def _laplacian(a: np.ndarray) -> np.ndarray:
    """4-neighbour discrete Laplacian via slicing (no scipy dependency)."""
    return (
        -4.0 * a[1:-1, 1:-1]
        + a[:-2, 1:-1]
        + a[2:, 1:-1]
        + a[1:-1, :-2]
        + a[1:-1, 2:]
    )


def _tile_origins(size: int, tile: int, count: int) -> list[int]:
    """Evenly spaced tile start offsets along one axis."""
    if size <= tile:
        return [0]
    usable = size - tile
    n = min(count, max(1, usable // tile + 1))
    if n == 1:
        return [usable // 2]
    step = usable / (n - 1)
    return [int(round(i * step)) for i in range(n)]


def _sample_tiles(luma: np.ndarray) -> list[np.ndarray]:
    h, w = luma.shape
    ys = _tile_origins(h, TILE, GRID)
    xs = _tile_origins(w, TILE, GRID)
    tiles = []
    for y in ys:
        for x in xs:
            tile = luma[y : y + TILE, x : x + TILE]
            if tile.shape[0] >= 16 and tile.shape[1] >= 16:
                tiles.append(tile)
    return tiles or [luma]


def _focus_score(tiles: list[np.ndarray]) -> tuple[float, float]:
    """Return (focus_score, focus_spread).

    Per tile: variance of the Laplacian, normalised by the tile's own mean
    luminance. The normalisation makes a correctly-exposed dark scene
    comparable with a bright one -- without it, a low-key image reads as
    "soft" purely because it's dark.

    ``focus_spread`` is the ratio of the top percentile to the median, which
    separates genuine shallow depth of field (high spread: something is sharp,
    much is not) from a uniformly soft frame (low spread: nothing is sharp).
    """
    scores = []
    for tile in tiles:
        if tile.shape[0] < 3 or tile.shape[1] < 3:
            continue
        lap = _laplacian(tile)
        mean = float(tile.mean())
        # +8 keeps near-black tiles from exploding the ratio.
        scores.append(float(lap.var()) / (mean + 8.0) * 16.0)
    if not scores:
        return 0.0, 1.0
    arr = np.asarray(scores, dtype=np.float64)
    top = float(np.percentile(arr, FOCUS_PERCENTILE))
    median = float(np.median(arr))
    spread = top / median if median > 1e-6 else float("inf") if top > 0 else 1.0
    return top, min(spread, 999.0)


def _noise_sigma(tiles: list[np.ndarray]) -> float:
    """Immerkaer noise estimate, taken over the flattest tiles only.

    The 3x3 kernel [[1,-2,1],[-2,4,-2],[1,-2,1]] annihilates any locally linear
    signal, so what survives is (mostly) noise. Restricting to the flattest
    tiles matters: run over textured tiles, fine detail such as foliage or
    fabric weave reads as noise and every landscape looks unusably grainy.
    """
    if not tiles:
        return 0.0
    usable = [t for t in tiles if t.shape[0] >= 8 and t.shape[1] >= 8]
    if not usable:
        return 0.0

    # Flatness = variance of the tile; take the smoothest third.
    variances = [float(t.var()) for t in usable]
    order = np.argsort(variances)
    keep = max(1, len(usable) // 3)
    flat = [usable[i] for i in order[:keep]]

    sigmas = []
    for tile in flat:
        a = tile
        conv = (
            a[:-2, :-2] - 2 * a[:-2, 1:-1] + a[:-2, 2:]
            - 2 * a[1:-1, :-2] + 4 * a[1:-1, 1:-1] - 2 * a[1:-1, 2:]
            + a[2:, :-2] - 2 * a[2:, 1:-1] + a[2:, 2:]
        )
        h, w = conv.shape
        if h <= 0 or w <= 0:
            continue
        sigma = float(np.abs(conv).sum()) * np.sqrt(np.pi / 2.0) / (6.0 * h * w)
        sigmas.append(sigma)
    return float(np.median(sigmas)) if sigmas else 0.0


def _exposure(luma_small: np.ndarray) -> tuple[float, float, float, float]:
    """(clip_low, clip_high, mean_luma, contrast) from a thumbnail histogram."""
    flat = luma_small.ravel()
    total = flat.size or 1
    clip_low = float(np.count_nonzero(flat <= _CLIP_DARK)) / total
    clip_high = float(np.count_nonzero(flat >= _CLIP_BRIGHT)) / total
    return clip_low, clip_high, float(flat.mean()), float(flat.std())


def _interpret(
    blur: float, spread: float, noise: float, clip_low: float,
    clip_high: float, mean_luma: float, contrast: float,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Turn raw numbers into flags and photographer-readable notes."""
    flags: list[str] = []
    notes: list[str] = []

    if blur < DEFAULT_MIN_BLUR:
        if spread > 4.0:
            notes.append(
                "much of the frame is soft, but part of it is sharp - looks like "
                "intentional shallow depth of field"
            )
        else:
            flags.append("soft")
            notes.append(f"nothing in the frame measures as critically sharp (focus {blur:.0f})")
    elif spread > 8.0:
        notes.append("strong focus falloff - shallow depth of field")

    if noise > DEFAULT_MAX_NOISE:
        flags.append("noisy")
        notes.append(f"visible noise in flat areas (sigma {noise:.1f})")

    if clip_high > DEFAULT_MAX_CLIPPING:
        flags.append("blown-highlights")
        notes.append(f"{clip_high * 100:.1f}% of pixels are blown to pure white")
    if clip_low > DEFAULT_MAX_CLIPPING:
        flags.append("crushed-shadows")
        notes.append(f"{clip_low * 100:.1f}% of pixels are crushed to pure black")

    # High-key and low-key are legitimate styles, so these stay notes, never flags.
    if mean_luma < 45:
        notes.append("very dark overall (low-key)")
    elif mean_luma > 210:
        notes.append("very bright overall (high-key)")
    if contrast < 18:
        notes.append("low contrast - may look flat")

    return tuple(flags), tuple(notes)


def analyze_array(rgb: Image.Image) -> QualityReport:
    """Measure an already-decoded, upright, sRGB image."""
    luma = _to_luma(rgb)
    tiles = _sample_tiles(luma)

    blur, spread = _focus_score(tiles)
    noise = _noise_sigma(tiles)

    # Exposure on a thumbnail: scale-invariant and much cheaper.
    h, w = luma.shape
    if max(h, w) > HISTOGRAM_EDGE:
        step = int(np.ceil(max(h, w) / HISTOGRAM_EDGE))
        luma_small = luma[::step, ::step]
    else:
        luma_small = luma
    clip_low, clip_high, mean_luma, contrast = _exposure(luma_small)

    flags, notes = _interpret(blur, spread, noise, clip_low, clip_high, mean_luma, contrast)
    return QualityReport(
        blur_score=blur,
        noise_score=noise,
        clip_low=clip_low,
        clip_high=clip_high,
        mean_luma=mean_luma,
        contrast=contrast,
        flags=flags,
        notes=notes,
        measured=True,
    )


def analyze_quality(path: Path) -> QualityReport:
    """Measure the file at ``path``. Never raises -- quality analysis is a
    signal, not a gate, and must not be able to break a run."""
    from . import loader

    try:
        with loader.open_image(path) as img:
            return analyze_array(img)
    except Exception as exc:
        log.debug("Quality analysis failed for %s: %s", path, exc)
        return unmeasured(str(exc))


def unmeasured(reason: str = "") -> QualityReport:
    """A report that explicitly carries no measurement."""
    return QualityReport(
        blur_score=0.0,
        noise_score=0.0,
        clip_low=0.0,
        clip_high=0.0,
        mean_luma=0.0,
        contrast=0.0,
        flags=(),
        notes=(f"quality analysis unavailable: {reason}",) if reason else (),
        measured=False,
    )


def describe_for_prompt(q: QualityReport) -> str:
    """Render measurements as ground truth for the model prompt.

    Phrased as instrument readings rather than conclusions, so the model treats
    them as evidence rather than being told what verdict to reach.
    """
    if not q.measured:
        return ""
    parts = [
        f"focus score {q.blur_score:.0f} (measured at native resolution; "
        f"below {DEFAULT_MIN_BLUR:.0f} indicates nothing critically sharp)",
        f"noise sigma {q.noise_score:.1f} in flat areas",
        f"{q.clip_high * 100:.1f}% of pixels blown to white, "
        f"{q.clip_low * 100:.1f}% crushed to black",
        f"mean brightness {q.mean_luma:.0f}/255, contrast {q.contrast:.0f}",
    ]
    text = "MEASURED TECHNICAL DATA for this exact image, taken from the "
    text += "full-resolution original before any downsampling:\n- " + "\n- ".join(parts)
    if q.notes:
        text += "\nAutomated observations: " + "; ".join(q.notes)
    text += (
        "\nTrust these figures over your own impression of sharpness and noise: "
        "the image you were shown is a downsampled preview in which that "
        "information is no longer present."
    )
    return text
