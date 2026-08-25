"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import limits as limits_mod
from .config import VERSION, Settings, load_settings
from .errors import ConfigError, RegistryError, StockFlowError
from .imaging import loader
from .metadata import ExifToolWriter
from .models import FOLDER_READY, FOLDER_REPORTS
from .pipeline import Pipeline
from .reporting import format_summary

log = logging.getLogger("stockflow")


def make_console_safe() -> None:
    """Stop a non-ASCII filename or title from killing the run.

    On a default Windows console, ``print`` encodes with cp1252 and raises
    UnicodeEncodeError on any CJK or accented character. v4 printed every
    filename unguarded, so a single ``日本の桜.jpg`` aborted the whole batch.
    Reconfiguring to UTF-8 with replacement means the worst case is a mangled
    glyph on screen, never a lost run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging(settings: Settings) -> None:
    level = logging.DEBUG if settings.verbose else logging.WARNING if settings.quiet else logging.INFO

    root = logging.getLogger("stockflow")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    if not settings.dry_run:
        try:
            settings.reports_dir.mkdir(parents=True, exist_ok=True)
            debug_log = settings.reports_dir / "stockflow.log"
            handler = logging.FileHandler(debug_log, encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-10s %(name)s: %(message)s")
            )
            root.addHandler(handler)
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stockflow",
        description="Prepare stock photography for Shutterstock, Adobe Stock and similar marketplaces.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  stockflow "D:\\Photos\\batch_01"\n'
            '  stockflow "D:\\Photos\\batch_01" --dry-run\n'
            '  stockflow "D:\\Photos\\batch_01" --limit 200 --workers 4 --min-score 70\n'
            '  stockflow "D:\\Photos\\batch_01" --retry-failed\n'
        ),
    )
    p.add_argument("folder", nargs="?", help="Folder containing the photos to process.")
    p.add_argument("--version", action="version", version=f"StockFlow {VERSION}")

    g = p.add_argument_group("selection")
    g.add_argument("--limit", type=int, dest="batch_limit", metavar="N",
                   help="Maximum images to process this run (default 50).")
    g.add_argument("--retry-failed", action="store_true", default=None,
                   help="Also retry images previously marked ERROR or ERROR_PERMANENT.")

    g = p.add_argument_group("model")
    g.add_argument("--model", help="Gemini model id (default gemini-2.5-flash-lite).")
    g.add_argument("--workers", type=int, metavar="N",
                   help="Concurrent workers (default 3).")
    g.add_argument("--rpm", type=int, metavar="N",
                   help="Requests-per-minute cap. Overrides the built-in estimate.")
    g.add_argument("--rpd", type=int, metavar="N",
                   help="Requests-per-day cap. Overrides the built-in estimate.")

    g = p.add_argument_group("thresholds")
    g.add_argument("--min-score", type=int, metavar="N",
                   help="Commercial score below which an image is set aside (default 60).")
    g.add_argument("--min-megapixels", type=float, metavar="MP",
                   help="Reject anything below this resolution (default 4.0).")
    g.add_argument("--min-blur", type=float, metavar="N",
                   help="Reject images whose measured focus score is below N. "
                        "Off by default; measured and reported either way.")
    g.add_argument("--max-noise", type=float, metavar="N",
                   help="Reject images whose measured noise sigma exceeds N. Off by default.")
    g.add_argument("--max-clipping", type=float, metavar="FRACTION",
                   help="Reject images with more than this fraction of pure black/white "
                        "pixels, e.g. 0.05. Off by default.")

    g = p.add_argument_group("behaviour")
    g.add_argument("--dry-run", action="store_true", default=None,
                   help="Report what would happen. Moves no files and spends no API quota.")
    g.add_argument("--no-move", action="store_true", default=None,
                   help="Write metadata and reports but leave files where they are.")
    g.add_argument("--force-fresh-registry", action="store_true", default=None,
                   help="Start over if the registry is corrupt (it is moved aside first).")
    g.add_argument("--max-filename", type=int, dest="max_filename_chars", metavar="N",
                   help="Maximum output filename length including extension (default 30, "
                        "which is Adobe Stock's limit). Raise it if you only use Shutterstock.")
    g.add_argument("--exiftool-path", help="Full path to the exiftool binary.")
    g.add_argument("--config", type=Path, help="JSON config file (default: stockflow.json in the folder).")

    g = p.add_argument_group("output")
    g.add_argument("-v", "--verbose", action="store_true", default=None, help="Show debug detail.")
    g.add_argument("-q", "--quiet", action="store_true", default=None, help="Only warnings and errors.")

    return p


def print_banner(settings: Settings, writer: ExifToolWriter, quota, model_limits) -> None:
    support = loader.describe_support()
    exif_version = writer.version()
    print("=" * 66)
    print(f"StockFlow v{VERSION}")
    print(f"  Folder            : {settings.folder}")
    print(f"  Model             : {settings.model}")
    print(f"  Workers           : {settings.workers}")
    print(
        f"  Rate limit        : {settings.rpm or model_limits.rpm}/min, "
        f"{settings.rpd or model_limits.rpd}/day  [{model_limits.source}]"
    )
    print(f"  Daily quota used  : {quota.used}/{quota.limit}")
    print(f"  Batch limit       : {settings.batch_limit}")
    print(f"  Quality threshold : {settings.min_score}/100")
    print(f"  Resolution min    : {settings.min_megapixels}MP")
    gates = [
        f"blur<{settings.min_blur}" if settings.min_blur else "",
        f"noise>{settings.max_noise}" if settings.max_noise else "",
        f"clip>{settings.max_clipping}" if settings.max_clipping else "",
    ]
    active = ", ".join(g for g in gates if g)
    print(f"  Local gates       : {active if active else 'measure and report only'}")
    print(f"  ExifTool          : {exif_version or 'NOT FOUND'}  ({settings.exiftool_path})")
    print(
        f"  Formats           : JPEG/PNG/TIFF/WebP"
        f"{', HEIC' if support['heic/heif'] else ''}"
        f"{', RAW' if support['raw'] else ''}"
    )
    if not support["raw"]:
        print("                      RAW support off - install with: pip install stockflow[raw]")
    if settings.dry_run:
        print("  MODE              : DRY RUN - no files moved, no API calls")
    print("=" * 66)


def main(argv: list[str] | None = None) -> int:
    make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.folder:
        parser.print_help()
        return 2

    cli_args = {k: v for k, v in vars(args).items() if v is not None and k != "config"}

    try:
        settings = load_settings(cli_args, config_path=args.config)
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings)

    writer = ExifToolWriter(settings.exiftool_path)
    if not settings.dry_run and not writer.available():
        print(
            f"\nCannot run exiftool ({settings.exiftool_path}).\n"
            f"Put exiftool.exe beside stockflow.py, set STOCKFLOW_EXIFTOOL to its full "
            f"path, or pass --exiftool-path.\n",
            file=sys.stderr,
        )
        return 3

    analyzer = None
    if not settings.dry_run:
        if not settings.api_key:
            print(
                "\nGEMINI_API_KEY is not set.\n"
                'In PowerShell:  setx GEMINI_API_KEY "your-key-here"\n'
                "then open a new terminal and try again.\n"
                "Or run with --dry-run to inspect the folder without any API calls.\n",
                file=sys.stderr,
            )
            return 4

    stop = threading.Event()
    _install_signal_handler(stop)

    try:
        from .ratelimit import AdaptiveGate, TokenBucket

        model_limits = limits_mod.for_model(settings.model)
        bucket = TokenBucket(settings.rpm or model_limits.rpm)
        gate = AdaptiveGate()

        if settings.dry_run:
            from .analyzer import FakeAnalyzer

            analyzer = FakeAnalyzer()
        else:
            from .analyzer import GeminiAnalyzer

            analyzer = GeminiAnalyzer(
                settings.api_key,
                settings.model,
                bucket=bucket,
                gate=gate,
                max_retries=settings.max_retries,
                stop=stop,
            )

        pipeline = Pipeline(
            settings, analyzer, writer, stop=stop, bucket=bucket, gate=gate,
            progress=lambda msg: print(msg, flush=True),
        )
        if not settings.dry_run:
            analyzer._on_quota_observed = lambda qid, val: _observe_quota(pipeline, qid, val)

        if not settings.quiet:
            print_banner(settings, writer, pipeline.quota, model_limits)

        result = pipeline.run()

    except RegistryError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("\nInterrupted. Progress so far has been saved; run again to continue.")
        return 130
    except StockFlowError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    _print_outcome(result, settings)
    return 0


def _observe_quota(pipeline: Pipeline, quota_id: str, value: int) -> None:
    """Fold a real quota value from a 429 back into our limiter."""
    updated = limits_mod.observe_quota_value(pipeline.model_limits, quota_id, value)
    if updated is pipeline.model_limits:
        return
    pipeline.model_limits = updated
    log.warning("API reported a real limit: %s = %s. Adjusting.", quota_id, value)
    if "perminute" in quota_id.lower() and "token" not in quota_id.lower():
        pipeline.bucket.update_rate(updated.rpm)
    elif "perday" in quota_id.lower():
        pipeline.quota.set_limit(updated.rpd)


def _install_signal_handler(stop: threading.Event) -> None:
    def handler(signum, frame):  # noqa: ARG001
        if stop.is_set():
            raise KeyboardInterrupt
        print("\nStopping after the images already in flight... (Ctrl-C again to force)")
        stop.set()

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):  # not on the main thread
        pass


def _print_outcome(result, settings: Settings) -> None:
    if settings.dry_run:
        print("\n-- Dry run " + "-" * 44)
        print(f"   Would process        : {len(result.records)}")
        print("   No files were moved and no API quota was spent.")
        print("   Re-run without --dry-run to process for real.")
        return

    print(format_summary(result.summary))

    reports = settings.reports_dir
    print(f"   Ready folder          : {settings.folder / FOLDER_READY}")
    print(f"   Shutterstock CSV      : {reports / 'shutterstock_upload.csv'}")
    print(f"   Adobe Stock CSV       : {reports / 'adobe_stock_upload.csv'}")
    print("     (Adobe's Releases column needs the release filenames you uploaded"
          " to their portal - StockFlow leaves it blank)")
    if (reports / "needs_review.txt").exists():
        print(f"   Review log            : {reports / 'needs_review.txt'}")
    if result.report_paths.get("run_json"):
        print(f"   Run report            : {result.report_paths['run_json']}")

    if result.stopped_on_quota:
        print(
            "\n   Stopped early: today's model quota is spent. Nothing was lost -- "
            "\n   everything still pending will be picked up next run. The free-tier "
            "\n   quota resets at midnight US Pacific."
        )
    if result.summary.get("remaining"):
        print(f"\n   {result.summary['remaining']} image(s) still pending. Run again to continue.")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
