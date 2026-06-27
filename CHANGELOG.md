# Changelog

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
