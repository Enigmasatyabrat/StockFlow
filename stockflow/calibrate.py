"""Deriving quality thresholds from a real portfolio instead of guessing.

The defaults in :mod:`stockflow.imaging.quality` are derived from the
mathematics, not from a labelled dataset. They are honest starting points and
nothing more, which is exactly why the gates they feed are off by default.

This module closes that gap. Point it at photographs you have already judged
and it reports how the measurements actually distribute across *your* work,
then suggests thresholds drawn from that distribution.

Two modes:

* **One folder** -- report the distribution and suggest a percentile cut.
  Useful when you have a pile of mixed work and want to reject the worst few
  percent.
* **Two folders** (``--against``) -- you supply images you consider good and
  images you consider bad, and it finds the threshold that best separates
  them, reporting honestly how cleanly they separate at all.

No API calls. No files are moved. This only reads pixels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import Settings
from .imaging import loader, quality as quality_mod
from .models import QualityReport

log = logging.getLogger(__name__)


@dataclass
class Measurement:
    path: Path
    report: QualityReport

    @property
    def name(self) -> str:
        return self.path.name


def measure_folder(
    folder: Path,
    settings: Settings,
    *,
    limit: int = 0,
    progress: Callable[[str], None] | None = None,
) -> list[Measurement]:
    """Measure every supported image directly inside ``folder``."""
    progress = progress or (lambda msg: None)
    paths = list(loader.iter_source_files(folder, settings))
    if limit:
        paths = paths[:limit]

    out: list[Measurement] = []
    for index, path in enumerate(paths, 1):
        report = quality_mod.analyze_quality(path)
        if not report.measured:
            progress(f"  [{index}/{len(paths)}] {path.name} ... could not measure, skipped")
            continue
        out.append(Measurement(path, report))
        if index % 10 == 0 or index == len(paths):
            progress(f"  measured {index}/{len(paths)}")
    return out


def percentiles(values: Sequence[float], points=(0, 5, 10, 25, 50, 75, 90, 100)) -> dict[int, float]:
    if not values:
        return {p: 0.0 for p in points}
    import numpy as np

    arr = np.asarray(sorted(values), dtype=float)
    return {p: float(np.percentile(arr, p)) for p in points}


def _fmt(value: float) -> str:
    return f"{value:,.1f}" if value >= 10 else f"{value:.2f}"


def describe_distribution(
    measurements: Sequence[Measurement], cut_percentile: int = 5
) -> str:
    """Human-readable distribution report plus suggested thresholds."""
    if not measurements:
        return "No images could be measured."

    focus = [m.report.blur_score for m in measurements]
    noise = [m.report.noise_score for m in measurements]
    clip = [m.report.clip_low + m.report.clip_high for m in measurements]

    fp = percentiles(focus)
    np_ = percentiles(noise)
    cp = percentiles(clip)

    lines = [
        f"Measured {len(measurements)} image(s).",
        "",
        "  metric        min       p5      p25   median      p75      max",
        "  " + "-" * 62,
        f"  focus    {_fmt(fp[0]):>9} {_fmt(fp[5]):>8} {_fmt(fp[25]):>8} "
        f"{_fmt(fp[50]):>8} {_fmt(fp[75]):>8} {_fmt(fp[100]):>8}",
        f"  noise    {_fmt(np_[0]):>9} {_fmt(np_[5]):>8} {_fmt(np_[25]):>8} "
        f"{_fmt(np_[50]):>8} {_fmt(np_[75]):>8} {_fmt(np_[100]):>8}",
        f"  clipping {_fmt(cp[0] * 100):>9} {_fmt(cp[5] * 100):>8} {_fmt(cp[25] * 100):>8} "
        f"{_fmt(cp[50] * 100):>8} {_fmt(cp[75] * 100):>8} {_fmt(cp[100] * 100):>8}   (%)",
        "",
    ]

    suggested_blur = percentiles(focus, (cut_percentile,))[cut_percentile]
    suggested_noise = percentiles(noise, (100 - cut_percentile,))[100 - cut_percentile]
    suggested_clip = percentiles(clip, (100 - cut_percentile,))[100 - cut_percentile]

    lines += [
        f"Suggested gates, cutting the worst {cut_percentile}% of THIS set:",
        "",
        f"  --min-blur {suggested_blur:.0f} --max-noise {suggested_noise:.1f} "
        f"--max-clipping {suggested_clip:.3f}",
        "",
        "These are descriptive, not prescriptive: they describe where the worst",
        f"{cut_percentile}% of these particular images sit. If this folder is all",
        "good work, cutting the bottom 5% throws away good work. Look at the",
        "images listed below before trusting any of it.",
        "",
        f"The built-in defaults for comparison: --min-blur "
        f"{quality_mod.DEFAULT_MIN_BLUR:.0f} --max-noise "
        f"{quality_mod.DEFAULT_MAX_NOISE:.1f} --max-clipping "
        f"{quality_mod.DEFAULT_MAX_CLIPPING:.2f}",
    ]

    # Say so plainly when the shipped defaults are badly wrong for this
    # material. They are derived from the mathematics, not from photographs,
    # and this is exactly the situation calibration exists to expose.
    would_flag = sum(1 for f in focus if f < quality_mod.DEFAULT_MIN_BLUR)
    share = would_flag / len(focus)
    if share > 0.5:
        lines += [
            "",
            f"NOTE: the default --min-blur {quality_mod.DEFAULT_MIN_BLUR:.0f} would flag "
            f"{would_flag} of {len(focus)} images here ({share * 100:.0f}%) as soft.",
            "That default is a mathematical starting point, not a measurement of",
            "real photographs, and for this material it is clearly too high. Use a",
            "number from the table above, or leave the blur gate off entirely.",
        ]
    return "\n".join(lines)


def worst_by_focus(measurements: Sequence[Measurement], count: int = 10) -> list[Measurement]:
    return sorted(measurements, key=lambda m: m.report.blur_score)[:count]


def worst_by_noise(measurements: Sequence[Measurement], count: int = 10) -> list[Measurement]:
    return sorted(measurements, key=lambda m: -m.report.noise_score)[:count]


@dataclass
class Separation:
    """How cleanly a metric separates two labelled sets."""

    metric: str
    threshold: float
    #: Fraction of the "good" set correctly kept.
    kept_good: float
    #: Fraction of the "bad" set correctly rejected.
    rejected_bad: float
    #: Overall accuracy at this threshold.
    accuracy: float
    #: True when the two sets overlap so much that no threshold works well.
    overlapping: bool

    @property
    def usable(self) -> bool:
        return not self.overlapping and self.accuracy >= 0.7


def best_threshold(
    good: Sequence[float], bad: Sequence[float], *, higher_is_better: bool, metric: str
) -> Separation:
    """Find the cut that best separates two labelled sets.

    Sweeps every candidate value and keeps the best accuracy. Reports overlap
    honestly rather than returning a confident-looking number for two sets that
    a single threshold cannot separate -- which is the common case for noise,
    where a noisy-but-wanted night shot sits right on top of a rejected one.
    """
    if not good or not bad:
        return Separation(metric, 0.0, 0.0, 0.0, 0.0, overlapping=True)

    candidates = sorted(set(list(good) + list(bad)))
    best = Separation(metric, 0.0, 0.0, 0.0, 0.0, overlapping=True)

    for cut in candidates:
        if higher_is_better:
            kept = sum(1 for v in good if v >= cut) / len(good)
            rejected = sum(1 for v in bad if v < cut) / len(bad)
        else:
            kept = sum(1 for v in good if v <= cut) / len(good)
            rejected = sum(1 for v in bad if v > cut) / len(bad)
        accuracy = (kept * len(good) + rejected * len(bad)) / (len(good) + len(bad))
        if accuracy > best.accuracy:
            best = Separation(metric, cut, kept, rejected, accuracy, overlapping=False)

    # A threshold that only "works" by keeping everything or rejecting
    # everything has not separated anything.
    if best.kept_good < 0.5 or best.rejected_bad < 0.5:
        best = Separation(metric, best.threshold, best.kept_good, best.rejected_bad,
                          best.accuracy, overlapping=True)
    return best


def describe_separation(
    good: Sequence[Measurement], bad: Sequence[Measurement]
) -> str:
    """Compare a folder you consider good against one you consider bad."""
    if not good or not bad:
        return "Need images in both folders to compare."

    results = [
        best_threshold(
            [m.report.blur_score for m in good], [m.report.blur_score for m in bad],
            higher_is_better=True, metric="focus",
        ),
        best_threshold(
            [m.report.noise_score for m in good], [m.report.noise_score for m in bad],
            higher_is_better=False, metric="noise",
        ),
        best_threshold(
            [m.report.clip_low + m.report.clip_high for m in good],
            [m.report.clip_low + m.report.clip_high for m in bad],
            higher_is_better=False, metric="clipping",
        ),
    ]

    lines = [
        f"Comparing {len(good)} image(s) you kept against {len(bad)} you rejected.",
        "",
        "  metric     best cut   keeps good   rejects bad   accuracy",
        "  " + "-" * 58,
    ]
    for r in results:
        verdict = "" if r.usable else "   <- does not separate"
        lines.append(
            f"  {r.metric:<10} {r.threshold:>8.1f}  {r.kept_good * 100:>10.0f}%  "
            f"{r.rejected_bad * 100:>11.0f}%  {r.accuracy * 100:>8.0f}%{verdict}"
        )

    usable = [r for r in results if r.usable]
    lines += ["", ""]
    if usable:
        flags = []
        for r in usable:
            if r.metric == "focus":
                flags.append(f"--min-blur {r.threshold:.0f}")
            elif r.metric == "noise":
                flags.append(f"--max-noise {r.threshold:.1f}")
            else:
                flags.append(f"--max-clipping {r.threshold:.3f}")
        lines += ["Suggested:", "", "  " + " ".join(flags), ""]

    unusable = [r for r in results if not r.usable]
    if unusable:
        lines += [
            "These metrics do NOT separate your two sets: "
            + ", ".join(r.metric for r in unusable) + ".",
            "That is a real answer, not a failure. It means the difference you",
            "see in those images is not something this metric measures, and",
            "gating on it would reject the wrong pictures. Leave them off.",
        ]
    if not usable:
        lines += ["", "Nothing here separates cleanly. Keep all the local gates off."]
    return "\n".join(lines)
