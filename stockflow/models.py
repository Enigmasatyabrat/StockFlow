"""Pure data structures. No behaviour, no I/O.

Tier 0: depends only on the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Status(str, Enum):
    """Terminal classification for one source image.

    The *values* are byte-identical to the v4 string constants so registries
    written by older StockFlow versions keep matching after migration.
    """

    READY = "READY"
    LOW_RESOLUTION = "LOW_RESOLUTION"
    LOW_QUALITY = "LOW_QUALITY"
    DUPLICATE = "DUPLICATE"
    NEEDS_RELEASE = "NEEDS_RELEASE"
    REVIEW = "REVIEW"
    ERROR = "ERROR"
    ERROR_PERMANENT = "ERROR_PERMANENT"

    def __str__(self) -> str:  # keeps f-strings and log lines readable
        return self.value


#: Statuses that mean "this file is finished, never look at it again".
TERMINAL_STATUSES = frozenset(
    {
        Status.READY,
        Status.LOW_RESOLUTION,
        Status.LOW_QUALITY,
        Status.DUPLICATE,
        Status.NEEDS_RELEASE,
        Status.REVIEW,
        Status.ERROR_PERMANENT,
    }
)

# Output folder names. Kept identical to v4 so an existing working folder
# keeps its layout and .gitignore entries stay valid.
FOLDER_SOURCE_ORIGINALS = "00_SOURCE_ORIGINALS"
FOLDER_READY = "01_READY_UPLOAD"
FOLDER_LOWRES = "02_SKIPPED_LOWRES"
FOLDER_LOWQUALITY = "03_SKIPPED_LOWQUALITY"
FOLDER_DUPLICATES = "04_DUPLICATES"
FOLDER_NEEDS_RELEASE = "05_NEEDS_RELEASE"
FOLDER_REVIEW = "06_REVIEW"
FOLDER_ERRORS = "07_ERRORS"
FOLDER_REPORTS = "Reports"
WORK_DIRNAME = ".stockflow_work"

DESTINATION_FOLDERS = (
    FOLDER_SOURCE_ORIGINALS,
    FOLDER_READY,
    FOLDER_LOWRES,
    FOLDER_LOWQUALITY,
    FOLDER_DUPLICATES,
    FOLDER_NEEDS_RELEASE,
    FOLDER_REVIEW,
    FOLDER_ERRORS,
    FOLDER_REPORTS,
)

#: Where each status sends its file. v4 had no home for errors, which left
#: ERROR_PERMANENT files invisible in the scan root forever; 07_ERRORS fixes that.
STATUS_FOLDERS: dict[Status, str] = {
    Status.READY: FOLDER_READY,
    Status.LOW_RESOLUTION: FOLDER_LOWRES,
    Status.LOW_QUALITY: FOLDER_LOWQUALITY,
    Status.DUPLICATE: FOLDER_DUPLICATES,
    Status.NEEDS_RELEASE: FOLDER_NEEDS_RELEASE,
    Status.REVIEW: FOLDER_REVIEW,
    Status.ERROR_PERMANENT: FOLDER_ERRORS,
}


@dataclass(frozen=True)
class ImageFacts:
    """Cheap, header-only facts about a file. Reading these must not decode pixels."""

    width: int
    height: int
    fmt: str
    mode: str = "RGB"
    #: True when the pixels come from a RAW decoder rather than a PIL plugin.
    is_raw: bool = False
    #: EXIF orientation tag (1..8). 1 means "already upright".
    orientation: int = 1

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000


@dataclass(frozen=True)
class QualityReport:
    """Locally measured technical quality, computed on full-resolution pixels.

    Every number here is measured at a *fixed normalised scale* (see
    imaging.quality). Raw variance-of-Laplacian is ~53x larger at 24MP than at
    1024px for the same photograph, so an un-normalised threshold is
    meaningless -- measuring at a fixed scale is what makes these comparable
    across a mixed portfolio.
    """

    blur_score: float
    noise_score: float
    clip_low: float          # fraction of pixels crushed to black
    clip_high: float         # fraction of pixels blown to white
    mean_luma: float         # 0..255
    contrast: float          # std-dev of luma
    #: Machine-readable flags, e.g. ("soft", "underexposed"). Empty means clean.
    flags: tuple[str, ...] = ()
    #: Human-readable observations, always safe to show a photographer.
    notes: tuple[str, ...] = ()
    #: False when analysis could not run; callers must not gate on the numbers.
    measured: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "blur": round(self.blur_score, 2),
            "noise": round(self.noise_score, 3),
            "clip_low": round(self.clip_low, 5),
            "clip_high": round(self.clip_high, 5),
            "mean_luma": round(self.mean_luma, 1),
            "contrast": round(self.contrast, 2),
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class Analysis:
    """What the vision model returned, after validation and cleanup."""

    title: str
    description: str
    keywords: tuple[str, ...]
    category: str
    category2: str = ""
    commercial_score: int = 0
    rejection_risk: str = "Unknown"
    rejection_reason: str = ""
    people_visible: bool = False
    property_or_trademark_visible: bool = False
    watermark_or_overlay_visible: bool = False
    #: Populated from response.usage_metadata when the SDK provides it.
    prompt_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Decision:
    """The routing verdict for one image: where it goes and why."""

    status: Status
    reason: str

    @property
    def folder(self) -> str:
        return STATUS_FOLDERS.get(self.status, FOLDER_REVIEW)


@dataclass
class ItemRecord:
    """One row in the run report. Mutable because it's filled in stages."""

    original_name: str
    final_name: str = ""
    status: Status | None = None
    destination: str = ""
    reason: str = ""
    score: int | None = None
    risk: str = ""
    category: str = ""
    category2: str = ""
    title: str = ""
    keyword_count: int | None = None
    megapixels: float | None = None
    sha256: str = ""
    phash: str = ""
    duplicate_of: str = ""
    near_duplicate_of: str = ""
    people_visible: bool = False
    property_or_trademark_visible: bool = False
    watermark_or_overlay_visible: bool = False
    quality: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {
            "original_name": self.original_name,
            "final_name": self.final_name,
            "status": str(self.status) if self.status else "",
            "destination": self.destination,
            "reason": self.reason,
            "score": self.score,
            "risk": self.risk,
            "category": self.category,
            "category2": self.category2,
            "title": self.title,
            "keyword_count": self.keyword_count,
            "megapixels": self.megapixels,
            "sha256": self.sha256,
            "phash": self.phash,
            "duplicate_of": self.duplicate_of,
            "near_duplicate_of": self.near_duplicate_of,
            "people_visible": self.people_visible,
            "property_or_trademark_visible": self.property_or_trademark_visible,
            "watermark_or_overlay_visible": self.watermark_or_overlay_visible,
            "quality": self.quality,
        }
        if self.error:
            d["error"] = self.error
        return d


@dataclass(frozen=True)
class PreparedImage:
    """Result of the prep stage: decoded, normalised, measured, hashed."""

    source: Path
    #: The file that will actually be moved into the destination folder.
    #: Equal to `source` when no conversion was needed.
    work_path: Path
    facts: ImageFacts
    quality: QualityReport
    sha256: str
    phash: str | None
    #: Bytes to send to the model (already downsampled and sRGB).
    api_bytes: bytes
    #: True when work_path is a derived file inside .stockflow_work.
    derived: bool
    #: Real container format of work_path, e.g. "JPEG" or "TIFF". Drives the
    #: output extension so a TIFF never gets renamed to .jpg while still
    #: containing TIFF bytes.
    actual_format: str
