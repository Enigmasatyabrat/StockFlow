"""Upload CSVs, run reports, and the review log.

Thread-safe: every writer here takes a lock, because the pipeline appends rows
from worker threads.
"""

from __future__ import annotations

import csv
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# Both headers are verified against official contributor documentation -- see
# stockflow/marketplaces.py for the sources. They are external contracts:
# Shutterstock's docs warn that a CSV not formatted exactly as their sample
# "will be rejected".
#
# Note the deliberate and counter-intuitive Shutterstock mapping: the model's
# TITLE goes into the column named "Description", because Shutterstock's
# Description field is the searchable headline shown to buyers, not a caption.
# Adobe calls the equivalent field "Title".
from .marketplaces import (
    ADOBE_HEADER,
    SHUTTERSTOCK_HEADER,
    adobe_category_number,
    adobe_title,
)
from .models import ItemRecord, Status

log = logging.getLogger(__name__)


class UploadCsv:
    """Append-only CSV writer with a header written exactly once."""

    def __init__(self, path: Path, header: Sequence[str]):
        self.path = path
        self.header = list(header)
        self._lock = threading.Lock()

    def append(self, row: Sequence[Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists() or self.path.stat().st_size == 0
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if new:
                    writer.writerow(self.header)
                writer.writerow(list(row))


def shutterstock_row(record: ItemRecord, keywords: Sequence[str]) -> list[str]:
    categories = ", ".join(c for c in (record.category, record.category2) if c)
    return [record.final_name, record.title, ", ".join(keywords), categories]


def adobe_row(record: ItemRecord, keywords: Sequence[str]) -> list[str]:
    """One Adobe Stock CSV row.

    Two columns differ from Shutterstock in ways that are easy to get wrong:

    * **Category is a number**, not a name -- Adobe's sample row uses ``3``.
      Writing "Business/Finance" there is simply invalid. Unmappable
      categories are left blank so Adobe's own auto-suggestion applies.
    * **Releases holds the filenames of release documents** already uploaded
      to the contributor portal, not a flag. StockFlow cannot know those
      names, so the column is always left empty and the requirement is
      surfaced through 05_NEEDS_RELEASE and the review log instead.
    """
    return [
        record.final_name,
        adobe_title(record.title),
        ", ".join(keywords),
        adobe_category_number(record.category),
        "",
    ]


class ReviewLog:
    """Human-readable notes, always UTF-8 so non-ASCII titles survive."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")


REPORT_COLUMNS = [
    "original_name", "final_name", "status", "score", "risk", "category",
    "category2", "title", "destination", "reason", "megapixels",
    "people_visible", "property_or_trademark_visible",
    "watermark_or_overlay_visible", "keyword_count",
    "blur", "noise", "clip_low", "clip_high", "quality_flags",
    "duplicate_of", "near_duplicate_of", "sha256",
]


def _report_row(item: dict) -> list[Any]:
    quality = item.get("quality") or {}
    return [
        item.get("original_name", ""),
        item.get("final_name", ""),
        item.get("status", ""),
        item.get("score", ""),
        item.get("risk", ""),
        item.get("category", ""),
        item.get("category2", ""),
        item.get("title", ""),
        item.get("destination", ""),
        item.get("reason", ""),
        item.get("megapixels", ""),
        item.get("people_visible", False),
        item.get("property_or_trademark_visible", False),
        item.get("watermark_or_overlay_visible", False),
        item.get("keyword_count", ""),
        quality.get("blur", ""),
        quality.get("noise", ""),
        quality.get("clip_low", ""),
        quality.get("clip_high", ""),
        ", ".join(quality.get("flags", []) or []),
        item.get("duplicate_of", ""),
        item.get("near_duplicate_of", ""),
        item.get("sha256", ""),
    ]


def write_report(
    report_dir: Path, records: Sequence[dict], summary: dict, *, stamp: str | None = None
) -> dict[str, Path]:
    """Write this run's report, and refresh the ``latest`` convenience copies.

    v4 wrote ``report.json``/``report.csv`` in truncate mode, so every run
    destroyed the previous run's audit trail -- and since batches are capped,
    a large folder always takes several runs. Each run now gets its own
    timestamped file and the audit trail accumulates.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Two runs inside the same second must not overwrite each other -- that
    # would reintroduce exactly the audit-trail loss this function exists to
    # prevent.
    if (report_dir / f"report-{stamp}.json").exists():
        suffix = 2
        while (report_dir / f"report-{stamp}-{suffix}.json").exists():
            suffix += 1
        stamp = f"{stamp}-{suffix}"

    payload = {"summary": summary, "items": list(records)}
    written: dict[str, Path] = {}

    for name, path in (
        ("run_json", report_dir / f"report-{stamp}.json"),
        ("latest_json", report_dir / "report.json"),
    ):
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        written[name] = path

    for name, path in (
        ("run_csv", report_dir / f"report-{stamp}.csv"),
        ("latest_csv", report_dir / "report.csv"),
    ):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(REPORT_COLUMNS)
            for item in records:
                writer.writerow(_report_row(item))
        written[name] = path

    return written


def format_summary(summary: dict) -> str:
    """The end-of-run console block."""
    lines = [
        "",
        "-- Done " + "-" * 48,
        f"   Processed this run     : {summary.get('processed_this_run', 0)}",
        f"   Ready to upload        : {summary.get('ready_to_upload', 0)}",
        f"   Needs release          : {summary.get('needs_release', 0)}",
        f"   Review                 : {summary.get('review', 0)}",
        f"   Low quality            : {summary.get('low_quality', 0)}",
        f"   Low resolution         : {summary.get('low_res', 0)}",
        f"   Duplicates             : {summary.get('duplicates', 0)}",
        f"   Errors                 : {summary.get('errors', 0)}",
        f"   Still pending          : {summary.get('remaining', 0)}",
    ]
    if summary.get("average_commercial_score") is not None:
        lines.append(f"   Average score          : {summary['average_commercial_score']}/100")
    if summary.get("average_keyword_count") is not None:
        lines.append(f"   Average keyword count  : {summary['average_keyword_count']}")
    if summary.get("low_risk_ready_percent") is not None:
        lines.append(
            f"   Low-risk share of ready: {summary['low_risk_ready_percent']}%"
            f"  (not an acceptance guarantee - the marketplace's reviewers decide)"
        )
    lines.append(f"   Duration               : {summary.get('duration_seconds', 0)}s")
    lines.append(
        f"   API calls / retries    : {summary.get('api_calls', 0)} / {summary.get('api_retries', 0)}"
    )
    if summary.get("prompt_tokens"):
        lines.append(
            f"   Tokens in / out        : {summary['prompt_tokens']:,} / "
            f"{summary.get('output_tokens', 0):,}"
        )
    if summary.get("daily_quota_used") is not None:
        lines.append(
            f"   Daily quota used       : {summary['daily_quota_used']}/"
            f"{summary.get('daily_quota_limit', '?')} requests"
        )
    return "\n".join(lines)


def build_summary(
    records: Sequence[dict],
    *,
    counts: dict,
    remaining: int,
    duration: float,
    settings: Any,
    api_stats: dict,
    quota_used: int | None = None,
    quota_limit: int | None = None,
    stopped_on_quota: bool = False,
) -> dict:
    from .rules import summarize

    base = summarize(records)
    base.update(
        {
            "version": getattr(settings, "version", None) or __import__("stockflow").__version__,
            "model": settings.model,
            "folder": str(settings.folder),
            "processed_this_run": len(records),
            "low_res": counts.get(Status.LOW_RESOLUTION, 0),
            "low_quality": counts.get(Status.LOW_QUALITY, 0),
            "duplicates": counts.get(Status.DUPLICATE, 0),
            "needs_release": counts.get(Status.NEEDS_RELEASE, 0),
            "review": counts.get(Status.REVIEW, 0),
            "errors": counts.get(Status.ERROR, 0) + counts.get(Status.ERROR_PERMANENT, 0),
            "remaining": remaining,
            "duration_seconds": round(duration, 1),
            "api_calls": api_stats.get("calls", 0),
            "api_retries": api_stats.get("retries", 0),
            "api_failures": api_stats.get("failures", 0),
            "prompt_tokens": api_stats.get("prompt_tokens", 0),
            "output_tokens": api_stats.get("output_tokens", 0),
            "daily_quota_used": quota_used,
            "daily_quota_limit": quota_limit,
            "stopped_on_daily_quota": stopped_on_quota,
            "dry_run": settings.dry_run,
        }
    )
    return base
