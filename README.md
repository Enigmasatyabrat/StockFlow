# StockFlow

**Sort, score, and prepare stock photography for Shutterstock, Adobe Stock, and other marketplaces.**

StockFlow takes a folder of photos and does the tedious part: it measures each
image's real technical quality, asks a vision model to judge its commercial
prospects, writes searchable metadata into the file, sorts everything into
labelled folders, and produces a marketplace-ready CSV.

Everything it decides is explained in plain language, and nothing is deleted.

---

## What it does

**Sorting** — every image lands in exactly one folder, with a written reason:

| Folder | Meaning |
|---|---|
| `01_READY_UPLOAD` | Passed everything. Metadata embedded, listed in the CSV. |
| `02_SKIPPED_LOWRES` | Below the resolution floor. |
| `03_SKIPPED_LOWQUALITY` | Below the commercial bar, or a visible watermark. |
| `04_DUPLICATES` | Byte-identical to an image already processed. |
| `05_NEEDS_RELEASE` | A recognisable person, building, or trademark. |
| `06_REVIEW` | Worth a human look before uploading. |
| `07_ERRORS` | Could not be processed. |
| `00_SOURCE_ORIGINALS` | Your originals, kept whenever a converted copy shipped instead. |

**Measuring** — sharpness, noise, exposure clipping, and contrast are measured
on the full-resolution file with numpy, then handed to the model as ground
truth. These are physical measurements, not opinions.

**Writing** — title, description, and up to 50 keywords are embedded as IPTC
and XMP, so the metadata travels with the file to any marketplace.

**Formats** — JPEG, PNG, TIFF, WebP, HEIC/HEIF, and RAW (CR2, CR3, NEF, ARW,
DNG, ORF, RAF, RW2) with the optional `raw` extra. Wide-gamut files are
converted to sRGB properly, and EXIF rotation is baked into the pixels.

---

## Install

```bash
pip install -e .
```

For RAW support:

```bash
pip install -e ".[raw]"
```

You also need **ExifTool** — put `exiftool.exe` beside `stockflow.py`, or set
`STOCKFLOW_EXIFTOOL` to its full path.

Then set your Gemini API key:

```powershell
setx GEMINI_API_KEY "YOUR_API_KEY"
```

Open a new terminal afterwards so the variable is picked up.

---

## Use

**Always look before you leap.** A dry run reads the folder, measures every
image, and tells you exactly what would happen — without moving a single file
or spending any API quota:

```bash
stockflow "D:\Photos\batch_01" --dry-run
```

When it looks right, run it for real:

```bash
stockflow "D:\Photos\batch_01"
```

On Windows you can also just double-click `run_stockflow.bat`, or drag a
folder onto it. It previews first and asks before doing anything.

`python stockflow.py FOLDER` and `python -m stockflow FOLDER` both work too.

### Calibrating the quality thresholds

The blur/noise/clipping defaults are derived from the mathematics, not from
photographs, which is why they reject nothing unless you ask. To get numbers
from your own work instead:

```bash
stockflow "D:\Photos\portfolio" --calibrate
```

Better, if you have both: point it at images you kept and images you rejected,
and it finds the threshold that actually separates them.

```bash
stockflow "D:\Photos\kept" --calibrate --against "D:\Photos\rejected"
```

No API calls, nothing written, nothing moved.

### Useful options

```bash
stockflow FOLDER --limit 200 --workers 4     # bigger batch, more concurrency
stockflow FOLDER --min-score 75              # stricter commercial bar
stockflow FOLDER --min-blur 120              # also reject measurably soft images
stockflow FOLDER --retry-failed              # another go at previous failures
stockflow FOLDER --no-move                   # write metadata, leave files put
stockflow FOLDER --model gemini-2.5-flash    # a different model
```

Run `stockflow --help` for the full list.

Settings resolve in this order: **command-line flag → environment variable →
`stockflow.json` in the folder → built-in default.**

---

## Output

Inside your photo folder:

```
Reports/
  shutterstock_upload.csv     import this on Shutterstock
  adobe_stock_upload.csv      see the note below
  needs_review.txt            read this before uploading
  report.json / report.csv    the latest run, machine-readable
  report-<timestamp>.*        every previous run, kept
  stockflow.log               full debug log
```

Reports accumulate rather than overwrite, so the audit trail survives across
the several runs a large folder takes.

---

## Resuming, and what happens if it stops

StockFlow keeps `.pipeline_registry.json` in the folder and processes only
what is still pending, so you can stop and re-run freely. Interrupt it with
Ctrl-C and it finishes the images already in flight, then exits cleanly.

If the daily API quota runs out mid-batch, it stops and says so. Nothing is
lost and nothing is blamed on the photo — the remaining images stay pending.
Free-tier quotas reset at midnight US Pacific.

Every file move is journalled *before* it happens, so a crash or a power cut
mid-move is detected and repaired on the next run rather than leaving a photo
stranded.

---

## Requirements

* Python 3.11+
* ExifTool
* A Google Gemini API key

---

## Notes and limits

* **The scores are guidance, not a verdict.** Marketplace reviewers make the
  real decision. A high score is not an acceptance guarantee.
* **Both CSV formats are verified** against official contributor
  documentation (August 2026) — see `stockflow/marketplaces.py` for the
  per-rule sources. Two Adobe details are easy to get wrong and worth knowing:
  its `Category` column takes a **number** (1–21), not a name, and its
  `Releases` column takes the **filenames of release documents you already
  uploaded** to the contributor portal. StockFlow can't know those names, so
  it leaves that column blank — check `05_NEEDS_RELEASE/` and fill it in
  yourself before submitting.
* **Output filenames are capped at 30 characters** by default, because Adobe
  requires it and Shutterstock documents no limit — so one set of files
  uploads to both. Raise it with `--max-filename` if you only use
  Shutterstock. Nothing is lost: the descriptive text lives in the title and
  keywords, and nobody searches on a filename.
* **The Shutterstock→Adobe category mapping is a judgement call**, not an
  equivalence — the taxonomies don't line up. Shutterstock's "The Arts",
  "Education", "Miscellaneous" and "Vintage" have no honest Adobe counterpart,
  so those are left blank for Adobe's own auto-suggestion rather than forced
  into a wrong bucket. Each one is noted in the review log.
* **Rate limits are estimates.** Google no longer publishes a per-model limit
  table publicly. StockFlow ships conservative defaults, tells you where they
  came from in the startup banner, corrects them automatically when the API
  reports a real quota value, and lets you override with `--rpm` / `--rpd`.
* **Local quality gates are off by default.** Blur, noise, and clipping are
  always measured and reported, but nothing is rejected on those numbers
  unless you ask with `--min-blur`, `--max-noise`, or `--max-clipping`. The
  thresholds are starting points — tune them on your own portfolio.
* **Near-duplicates are flagged, never moved.** Two frames from a burst are
  often both worth submitting, so you decide.

---

## Documentation

| Document | Contents |
|---|---|
| [Why StockFlow exists](docs/why.md) | The problem it solves and the reasoning behind the design |
| [Architecture](docs/architecture.md) | Module map, design decisions, testing approach |
| [Reference](docs/reference.md) | Every flag, setting, folder and output file |
| [Troubleshooting](docs/troubleshooting.md) | When something looks wrong |

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

The suite runs with no API key, no network, and no real exiftool required
(tests that need the binary skip themselves).

---

## Roadmap

* Per-portfolio threshold calibration from your own accepted/rejected history
* Multi-agency upload support
* Contributor analytics

---

## License

Apache License 2.0.

## Author

Satyabrat Mishra

---

## Disclaimer

StockFlow assists with metadata generation and workflow automation.
Contributors remain responsible for verifying metadata accuracy and complying
with each marketplace's submission requirements, including model releases,
property releases, and intellectual property restrictions.
