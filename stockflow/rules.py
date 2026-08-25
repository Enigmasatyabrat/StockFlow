"""Pure decision logic. No I/O, no filesystem, no network, no logging.

Every function here is callable with plain values, which is what makes the
classification rules testable without fixtures.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Analysis, Decision, QualityReport, Status

#: Shutterstock's category list. `category` must be exactly one of these; the
#: model is constrained to them by the response schema, and anything that still
#: slips through is coerced to "Miscellaneous" rather than sent as-is.
SHUTTERSTOCK_CATEGORIES: tuple[str, ...] = (
    "Abstract", "Animals/Wildlife", "The Arts", "Backgrounds/Textures",
    "Beauty/Fashion", "Buildings/Landmarks", "Business/Finance", "Celebrities",
    "Education", "Food and Drink", "Healthcare/Medical", "Holidays",
    "Industrial", "Interiors", "Miscellaneous", "Nature", "Objects",
    "Parks/Outdoor", "People", "Religion", "Science", "Signs/Symbols",
    "Sports/Recreation", "Technology", "Transportation", "Vintage",
)
_CATEGORY_LOOKUP = {c.casefold(): c for c in SHUTTERSTOCK_CATEGORIES}

#: IPTC:Keywords truncates at 64 bytes per entry. Verified against exiftool
#: 13.52: an 80-character keyword is silently cut and only warns.
MAX_KEYWORD_BYTES = 64

#: Marketplaces cap keyword lists at 50.
MAX_KEYWORDS = 50

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Names Windows refuses regardless of extension.
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


# ---------------------------------------------------------------- coercion --

def as_bool(value: Any, default: bool = False) -> bool:
    """Coerce a model-supplied value to bool without the ``bool("false") is True`` trap.

    Vision models regularly return the *strings* ``"false"``/``"no"`` for
    boolean fields. ``bool()`` maps every non-empty string to True, which in v4
    silently routed clean images into NEEDS_RELEASE and, worse, made a
    ``"false"`` watermark flag mark good photos as low quality.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().casefold()
        if v in {"true", "yes", "y", "1"}:
            return True
        if v in {"false", "no", "n", "0", ""}:
            return False
        return default
    return default


def as_int(value: Any, default: int = 0, lo: int = 0, hi: int = 100) -> int:
    """Coerce to a clamped int. Tolerates "85", "85/100", 85.0, and None."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            n = int(round(float(value)))
        except (ValueError, OverflowError):
            return default
        return max(lo, min(hi, n))
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if not m:
            return default
        return max(lo, min(hi, int(round(float(m.group(0))))))
    return default


def as_text(value: Any) -> str:
    """Coerce to a stripped string, mapping None/null to ''.

    v4 called ``.strip()`` directly on ``data.get("rejection_reason", "")``,
    which raised AttributeError whenever the model returned JSON ``null`` --
    an entirely legal response for an optional field, and one that cost three
    billable retries per affected image.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize_risk(risk: Any) -> str:
    """Return canonical 'Low' / 'Medium' / 'High', or 'Unknown'.

    v4 compared the raw model string against ``{"Medium", "High"}``, so a
    response of ``"HIGH"`` or ``"high"`` fell through to READY -- exactly
    backwards from the intent.
    """
    text = as_text(risk).casefold()
    if text in _RISK_ORDER:
        return text.capitalize()
    for name in _RISK_ORDER:
        if name in text:
            return name.capitalize()
    return "Unknown"


def normalize_category(category: Any, category2: Any = "") -> tuple[str, str, list[str]]:
    """Coerce categories to the official list. Returns (cat, cat2, warnings)."""
    warnings: list[str] = []
    raw = as_text(category)
    cat = _CATEGORY_LOOKUP.get(raw.casefold(), "")
    if not cat:
        if raw:
            warnings.append(f"unrecognised category {raw!r}, using Miscellaneous")
        else:
            warnings.append("model returned no category, using Miscellaneous")
        cat = "Miscellaneous"

    raw2 = as_text(category2)
    cat2 = _CATEGORY_LOOKUP.get(raw2.casefold(), "")
    if raw2 and not cat2:
        warnings.append(f"unrecognised category2 {raw2!r}, dropped")
    if cat2 == cat:
        cat2 = ""
    return cat, cat2, warnings


# ---------------------------------------------------------------- keywords --

def clean_keywords(keywords: Iterable[Any], max_keywords: int = MAX_KEYWORDS) -> list[str]:
    """Normalise a model keyword list into something safe to embed and export.

    Beyond v4's dedupe/plural handling this also:

    * **splits on commas** -- a keyword containing a comma is indistinguishable
      from two keywords once written to IPTC, and it corrupts the CSV column,
      which is comma-joined. Verified against exiftool 13.52.
    * **caps each entry at 64 bytes**, the IPTC per-keyword limit, so exiftool
      never silently truncates.
    * collapses internal whitespace, which otherwise produces "sunset  beach"
      and "sunset beach" as two distinct keywords.
    """
    seen: set[str] = set()
    cleaned: list[str] = []

    for raw in keywords:
        text = as_text(raw)
        if not text:
            continue
        for part in text.split(","):
            kw = re.sub(r"\s+", " ", part).strip(" \t\n\r-_/")
            if len(kw) < 2:
                continue

            # Trim to the IPTC byte budget without splitting a UTF-8 sequence.
            encoded = kw.encode("utf-8")
            if len(encoded) > MAX_KEYWORD_BYTES:
                kw = encoded[:MAX_KEYWORD_BYTES].decode("utf-8", "ignore").strip()
                if len(kw) < 2:
                    continue

            key = kw.casefold()
            if key in seen:
                continue
            # Crude singular/plural collapse: keep whichever form arrived first.
            singular = key[:-1] if key.endswith("s") and len(key) > 3 else None
            if (singular and singular in seen) or (key + "s") in seen:
                continue

            seen.add(key)
            cleaned.append(kw)
            if len(cleaned) >= max_keywords:
                return cleaned
    return cleaned


# --------------------------------------------------------------- filenames --

def slugify(text: str, max_len: int = 80) -> str:
    """Filesystem-safe slug from a title.

    Keeps Unicode letters (a CJK title stays readable rather than collapsing to
    "image"), but strips combining marks, path separators, control characters
    and Windows-reserved names.
    """
    text = unicodedata.normalize("NFKC", str(text)).strip().casefold()
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")

    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    text = text.strip("-. ")

    if not text or text.casefold() in _RESERVED_NAMES:
        return "image" if not text else f"{text}-img"
    return text


#: Container format -> canonical extension.
_FORMAT_EXT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "TIFF": ".tif",
    "WEBP": ".webp",
    "HEIF": ".heic",
}


def target_filename(
    title: str,
    original: Path,
    status: Status,
    actual_format: str,
    max_chars: int = 30,
) -> str:
    """Final filename for a processed image, including the extension.

    ``actual_format`` is the *real* container of the bytes being moved, not a
    guess from the status. v4 hardcoded ``.jpg`` for READY/REVIEW/
    NEEDS_RELEASE/LOW_QUALITY, so a sub-50MB TIFF -- which ``normalize_image``
    returns untouched -- was renamed to ``.jpg`` while still containing TIFF
    bytes. Marketplaces reject those, and some editors refuse to open them.

    ``max_chars`` bounds the WHOLE filename, extension included, because Adobe
    Stock requires uploaded filenames to be 30 characters or fewer and to
    match the CSV exactly. Shutterstock documents no limit, so defaulting to
    the stricter of the two means one set of files uploads to both. The
    descriptive text lives in the title and keywords regardless -- nobody
    searches on a filename.
    """
    fmt = (actual_format or "").upper()
    ext = _FORMAT_EXT.get(fmt) or original.suffix.lower() or ".jpg"

    budget = max(1, max_chars - len(ext))
    base = slugify(title, max_len=budget) if as_text(title) else slugify(
        original.stem, max_len=budget
    )
    return f"{base}{ext}"


# ------------------------------------------------------------ the decision --

def choose_status(
    analysis: Analysis,
    quality: QualityReport | None,
    *,
    min_score: int = 60,
    min_blur: float | None = None,
    max_noise: float | None = None,
    max_clipping: float | None = None,
) -> Decision:
    """Route one analysed image, producing status and reason together.

    Status and reason are returned as a pair so they can never disagree --
    v4 computed them in two separate functions and a reason could describe a
    different verdict than the one applied.

    Order matters. Legal exposure outranks quality: a watermarked photo of an
    identifiable person is still a release problem, so releases are checked
    before the quality gates that would otherwise short-circuit.
    """
    risk = normalize_risk(analysis.rejection_risk)
    reason = as_text(analysis.rejection_reason)
    score = analysis.commercial_score

    # 1. Legal / release exposure.
    if analysis.people_visible or analysis.property_or_trademark_visible:
        what = []
        if analysis.people_visible:
            what.append("a recognisable person")
        if analysis.property_or_trademark_visible:
            what.append("property or a trademark")
        return Decision(
            Status.NEEDS_RELEASE,
            reason or f"Contains {' and '.join(what)} - needs a signed release before commercial use.",
        )

    # 2. Auto-reject conditions. Every marketplace rejects a visible watermark
    #    on sight, so there is no point spending review attention on it.
    if analysis.watermark_or_overlay_visible:
        return Decision(
            Status.LOW_QUALITY,
            reason or "Visible watermark, logo stamp or burned-in text - not sellable as-is.",
        )

    # 3. Locally measured technical faults. These are physical measurements on
    #    full-resolution pixels, so they outrank the model's opinion, which was
    #    formed from a downsampled preview. Only applied when the user opted in.
    if quality is not None and quality.measured:
        if min_blur is not None and quality.blur_score < min_blur:
            return Decision(
                Status.LOW_QUALITY,
                f"Measured softness: focus score {quality.blur_score:.0f} is below "
                f"the {min_blur:.0f} threshold.",
            )
        if max_noise is not None and quality.noise_score > max_noise:
            return Decision(
                Status.LOW_QUALITY,
                f"Measured noise level {quality.noise_score:.1f} exceeds the "
                f"{max_noise:.1f} threshold.",
            )
        if max_clipping is not None:
            clipped = quality.clip_low + quality.clip_high
            if clipped > max_clipping:
                return Decision(
                    Status.LOW_QUALITY,
                    f"{clipped * 100:.1f}% of pixels are clipped to pure black or white "
                    f"(limit {max_clipping * 100:.1f}%).",
                )

    # 4. Commercial bar.
    if score < min_score:
        return Decision(
            Status.LOW_QUALITY,
            reason or f"Commercial score {score}/100 is below the {min_score} threshold.",
        )

    # 5. Anything the model flagged as risky gets human eyes.
    if risk in {"Medium", "High"}:
        return Decision(
            Status.REVIEW,
            reason or f"Elevated rejection risk ({risk}) with no specific reason given - check manually.",
        )
    if risk == "Unknown":
        return Decision(
            Status.REVIEW,
            reason or f"Model returned an unrecognised risk value ({analysis.rejection_risk!r}) - check manually.",
        )

    # 6. Clean.
    note = ""
    if quality is not None and quality.measured and quality.notes:
        note = " Note: " + "; ".join(quality.notes)
    return Decision(Status.READY, f"Meets the quality bar (score {score}/100, risk {risk}).{note}")


def should_retry(status: Status | None, attempts: int, max_attempts: int) -> bool:
    """Whether a file with this registry state should be picked up again."""
    if status is None:
        return True
    if status in {Status.ERROR}:
        return attempts < max_attempts
    from .models import TERMINAL_STATUSES

    return status not in TERMINAL_STATUSES


def summarize(records: Sequence[dict]) -> dict:
    """Aggregate run statistics from item records."""
    from collections import Counter

    ready = [r for r in records if r.get("status") == str(Status.READY)]
    scored = [r for r in records if isinstance(r.get("score"), int)]
    kw = [r["keyword_count"] for r in ready if isinstance(r.get("keyword_count"), int)]
    low_risk = sum(1 for r in ready if r.get("risk") == "Low")

    return {
        "ready_to_upload": len(ready),
        "average_commercial_score": round(sum(r["score"] for r in scored) / len(scored), 1) if scored else None,
        "average_keyword_count": round(sum(kw) / len(kw), 1) if kw else None,
        "low_risk_ready_percent": round(100 * low_risk / len(ready), 1) if ready else None,
        "category_distribution": dict(Counter(r["category"] for r in ready if r.get("category"))),
        "status_distribution": dict(Counter(r.get("status", "") for r in records)),
    }
