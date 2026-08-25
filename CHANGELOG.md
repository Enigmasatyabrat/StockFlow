# Changelog

## v5.0.0 (2026-08-25)

A rewrite of the internals. The workflow, folder layout, registry filename and
Shutterstock CSV format are unchanged, so an in-progress folder keeps working.

### Fixed — data loss and corruption

* **The response schema was rejected by the API.** An empty string in the
  `category2` enum made Gemini refuse every request with
  `response_schema.properties[category2].enum[0]: cannot be empty`. Caught only
  by running against the live API — the whole fake-backed suite was green,
  because fakes never send the schema anywhere. `category2` is now a plain
  optional enum, and `TestResponseSchema` guards the whole class of mistake.

* **Files could be permanently stranded.** A file was moved to its destination
  before being recorded, so any failure in between (CSV open in Excel, full
  disk, sync lock) left it invisible to every later run. Moves are now
  journalled before they happen and repaired on the next run.
* **Keywords were duplicated on every re-run.** `-Keywords+=` appends;
  verified that running it twice produced `alpha, beta, alpha, gamma`, and
  that pre-existing Lightroom keywords were carried into submissions. Now a
  replacing write, and idempotent.
* **Non-ASCII metadata was silently corrupted.** Passed as command-line
  arguments, `café` became `caf?` and `日本の桜` became `????`, with IPTC
  fields truncated at the first non-ASCII byte and exiftool still exiting 0.
  All exiftool calls now go through a UTF-8 argfile, which also fixes unicode
  *filenames* and removes the Windows command-line length limit.
* **A non-ASCII filename or title crashed the whole run** with
  UnicodeEncodeError on a default Windows console.
* **Temporary files were never cleaned up** except on one code path, so every
  error leaked a full-resolution JPEG into `.stockflow_work`.
* **TIFFs were renamed to `.jpg`** while still containing TIFF bytes.
* **A corrupt registry silently reset the folder**, reprocessing everything
  and burning a whole day's quota. It now refuses to guess.
* **Reports were truncated every run**, destroying the audit trail.
* `"false"` from the model was truthy, `"HIGH"` didn't match `"High"`, a JSON
  `null` reason raised AttributeError, and a legacy lowercase status was never
  recognised. All fixed with defensive coercion.
* Duplicate detection now works **across runs** — hashes were being written to
  the registry but never read back.
* `requirements.txt` omitted `imagehash` and `numpy`; a clean install crashed
  on import.

### Added

* **Local quality measurement.** Sharpness, noise, exposure clipping and
  contrast measured on full-resolution pixels with numpy, then given to the
  model as ground truth. Previously the model was asked to grade sharpness
  from a 1024px JPEG where that information no longer existed. Measured at
  fixed-size native-resolution tiles so one threshold works across a mixed
  portfolio, and reported as a high percentile so shallow depth of field isn't
  mistaken for softness.
* **`--dry-run`** — measures everything and reports what would happen without
  moving a file or spending any quota.
* **HEIC/HEIF and RAW** (CR2, CR3, NEF, ARW, DNG, ORF, RAF, RW2 via the
  optional `raw` extra).
* **Colour management** — embedded ICC profiles are converted to sRGB properly
  instead of being discarded, and EXIF rotation is baked into the pixels.
* **Concurrency** with a shared token bucket and a pool-wide adaptive pause,
  replacing the flat 4-second sleep after every image.
* **A real CLI** — `--limit`, `--workers`, `--model`, `--min-score`,
  `--min-blur`, `--retry-failed`, `--no-move` and more, with
  flag → env → config file → default precedence.
* **Native structured output** with a response schema, replacing hand-parsing
  of the model's JSON.
* **Structured 429 handling** — per-day and per-minute exhaustion are told
  apart by `quotaId` rather than by the misleading `retryDelay`, and real
  quota values reported by the API are adopted automatically.
* `07_ERRORS/` so permanently failed files stop being invisible.
* **An Adobe Stock CSV**, with both headers verified against official
  contributor documentation and sources cited per rule in
  `stockflow/marketplaces.py`. Two Adobe columns are easy to get wrong and
  were: `Category` takes a **number** (1–21), not a category name, and
  `Releases` takes the filenames of release documents already uploaded to the
  portal, not a flag. Commas are stripped from Adobe titles as its docs
  require, and the Shutterstock→Adobe category mapping leaves genuinely
  unmappable categories blank rather than guessing.
* **Output filenames capped at 30 characters** (`--max-filename`), Adobe's
  documented limit, so one set of files uploads to both marketplaces. The
  de-collision suffix is absorbed by trimming rather than appended, so a
  second copy cannot quietly exceed the limit.
* Review-log `CHECK` lines for submission-format problems: over-long
  filenames, too few keywords, over-long titles, unmappable categories.
* A pytest suite: 332 tests, no API key or network required.

### Changed

* Restructured into a `stockflow/` package. `stockflow.py` remains as a
  launcher, so `python stockflow.py FOLDER` and `run_stockflow.bat` still work.
  `stockflow` and `python -m stockflow` are the preferred entry points.
* `run_stockflow.bat` now previews with `--dry-run` and asks before processing.
* Registry schema v2, migrated automatically with a backup written first.

---

## v4.1.1 (2026-06-27)

### Added

* Runtime diagnostics banner showing model, batch size, thresholds, ExifTool, and API status.
* Perceptual hash (pHash) based near-duplicate detection.
* Human-readable explanations for every image classification.
* Expanded processing reports with runtime, average score, keyword count, retry statistics, and category distribution.

### Improved

* Smarter Gemini retry logic for temporary server overloads (503).
* Separate handling of daily quota exhaustion (429).
* Stronger anti-hallucination prompt.
* More accurate release detection.
* Keyword cleanup (deduplication, singular/plural filtering, maximum 50 keywords).

### Fixed

* Daily quota exhaustion no longer blacklists images.
* Improved processing diagnostics and logging.
* More consistent report generation.

---

## v4.1.0 (2026-06-26)

### Added

* Automatic workflow folder organization.
* JSON and CSV reporting.
* SHA-256 duplicate detection.
* Automatic file renaming from generated titles.
* Upload-ready directory structure.
* Original file archiving.

### Improved

* Image preparation workflow.
* Batch processing pipeline.
* Metadata embedding.

---

## v4.0.0

### Added

* Automatic image preparation.
* PNG → JPEG conversion.
* Upload-ready image generation.
* Batch launcher.
* Improved metadata generation.

---

## v3.0.0

### Added

* Metadata embedding using ExifTool.
* Shutterstock CSV export.
* Adobe Stock metadata compatibility.
* Commercial scoring.
* AI-generated stock metadata.

---

## v2.0.0

### Added

* Improved Gemini metadata generation.
* Better validation.
* Retry handling.
* Improved prompt engineering.

---

## v1.0.0

### Initial Release

* AI-powered stock metadata generation.
* Basic Shutterstock workflow.
* Gemini Vision integration.
