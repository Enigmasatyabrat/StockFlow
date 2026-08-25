# StockFlow reference

## Command line

```
stockflow FOLDER [options]
python -m stockflow FOLDER [options]
python stockflow.py FOLDER [options]        # legacy shim, still works
```

### Selection

| Flag | Default | Meaning |
|---|---|---|
| `--limit N` | 50 | Maximum images processed this run |
| `--retry-failed` | off | Also retry images marked ERROR or ERROR_PERMANENT |

### Model

| Flag | Default | Meaning |
|---|---|---|
| `--model ID` | `gemini-2.5-flash-lite` | Vision model |
| `--workers N` | 3 | Concurrent workers |
| `--rpm N` | from `limits.py` | Requests-per-minute cap |
| `--rpd N` | from `limits.py` | Requests-per-day cap |

### Thresholds

| Flag | Default | Meaning |
|---|---|---|
| `--min-score N` | 60 | Commercial score below which an image is set aside |
| `--min-megapixels MP` | 4.0 | Resolution floor |
| `--min-blur N` | off | Reject measurably soft images |
| `--max-noise N` | off | Reject measurably noisy images |
| `--max-clipping F` | off | Reject when this fraction of pixels is pure black or white |

The three quality gates are **off by default**. Measurements are always taken
and always reported; nothing is rejected on them unless you opt in.

### Behaviour

| Flag | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Report only. No files moved, no API calls, nothing written |
| `--no-move` | off | Write metadata and reports, leave files where they are |
| `--max-filename N` | 30 | Output filename length cap, extension included |
| `--force-fresh-registry` | off | Start over if the registry is corrupt |
| `--exiftool-path P` | auto | Full path to the exiftool binary |
| `--config P` | `stockflow.json` in the folder | JSON config file |
| `-v` / `-q` | — | Verbose / quiet |

## Settings precedence

**CLI flag → environment variable → config file → default.**

Environment variables:

| Variable | Maps to |
|---|---|
| `GEMINI_API_KEY` | API key (required unless `--dry-run`) |
| `GEMINI_MODEL` | `--model` |
| `STOCKFLOW_WORKERS` | `--workers` |
| `STOCKFLOW_MIN_SCORE` | `--min-score` |
| `STOCKFLOW_MIN_MEGAPIXELS` | `--min-megapixels` |
| `STOCKFLOW_BATCH_LIMIT` | `--limit` |
| `STOCKFLOW_RPM` / `STOCKFLOW_RPD` | `--rpm` / `--rpd` |
| `STOCKFLOW_EXIFTOOL` | `--exiftool-path` |

A `stockflow.json` in the target folder is picked up automatically:

```json
{
  "min_score": 70,
  "workers": 4,
  "min_blur": 120,
  "max_filename_chars": 30
}
```

## Output folders

| Folder | Meaning |
|---|---|
| `00_SOURCE_ORIGINALS/` | Your originals, kept whenever a converted copy shipped |
| `01_READY_UPLOAD/` | Passed everything. Metadata embedded, listed in the CSVs |
| `02_SKIPPED_LOWRES/` | Below the resolution floor |
| `03_SKIPPED_LOWQUALITY/` | Below the commercial bar, or a visible watermark |
| `04_DUPLICATES/` | Byte-identical to an image already processed |
| `05_NEEDS_RELEASE/` | Identifiable person, building or trademark |
| `06_REVIEW/` | Worth a human look before uploading |
| `07_ERRORS/` | Could not be processed after `--limit` attempts |
| `Reports/` | CSVs, reports, review log, debug log |

## Files written

| File | Contents |
|---|---|
| `Reports/shutterstock_upload.csv` | `Filename, Description, Keywords, Categories` |
| `Reports/adobe_stock_upload.csv` | `Filename, Title, Keywords, Category, Releases` |
| `Reports/needs_review.txt` | Human-readable notes, flags, `CHECK` lines |
| `Reports/report.json` / `.csv` | Latest run, machine-readable |
| `Reports/report-<stamp>.*` | Every previous run, kept |
| `Reports/stockflow.log` | Full debug log |
| `.pipeline_registry.json` | Per-folder state — **do not delete** |
| `.stockflow_quota.json` | Daily API request counter |

### CSV notes

Shutterstock's `Description` column receives the **title**, not the
description. Their Description field is the searchable headline shown to
buyers, not a caption. This is counter-intuitive and deliberate.

Adobe's `Category` column takes a **number** 1–21, not a name. Its `Releases`
column takes the filenames of release documents you already uploaded to the
contributor portal — StockFlow cannot know those, so it leaves the column blank.
Fill it in yourself for anything from `05_NEEDS_RELEASE/`.

## Supported formats

| Format | Requires |
|---|---|
| JPEG, PNG, TIFF, WebP | nothing |
| HEIC, HEIF | `pillow-heif` (installed by default) |
| CR2, CR3, NEF, NRW, ARW, SRF, SR2, DNG, ORF, RAF, RW2, PEF | `pip install "stockflow[raw]"` |

RAW support degrades cleanly: without `rawpy` those files are simply not
listed as processable, and the startup banner says so.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A StockFlow error |
| 2 | Bad arguments or folder |
| 3 | exiftool not usable |
| 4 | `GEMINI_API_KEY` missing |
| 5 | Registry unreadable |
| 130 | Interrupted (Ctrl-C) — progress saved |
