# StockFlow troubleshooting

## Before anything else

```bash
stockflow "D:\Photos\batch" --dry-run
```

A dry run measures every image and reports exactly what would happen without
moving a file, writing a file or spending a single API call. If something looks
wrong, it will look wrong here first — for free.

## Setup problems

### `Cannot run exiftool`

StockFlow looks in this order: `--exiftool-path` → `STOCKFLOW_EXIFTOOL` → the
repository root (beside `stockflow.py`) → your `PATH`.

Put `exiftool.exe` beside `stockflow.py`, or:

```bash
setx STOCKFLOW_EXIFTOOL "C:\Tools\exiftool.exe"
```

### `GEMINI_API_KEY is not set`

```powershell
setx GEMINI_API_KEY "your-key"
```

Then **open a new terminal** — `setx` only affects processes started afterwards.
Or use `--dry-run`, which needs no key.

### `ModuleNotFoundError` on a fresh install

Install the package rather than only the requirements file:

```bash
pip install -e .
```

RAW support is a separate extra: `pip install -e ".[raw]"`.

### RAW files are ignored

The startup banner lists what this install can decode. If it says RAW support
is off, `rawpy` is not installed. This is intentional — `rawpy` ships platform
wheels that can fail to build, so it is optional and everything else works
without it.

## During a run

### It stopped partway and said the daily quota was reached

Working as intended. Nothing is lost and nothing is blamed on the photo — the
remaining images stay pending. Free-tier quotas reset at midnight US Pacific.

Run it again after that, or remove the ceiling entirely by adding billing to
your Google Cloud project. At measured usage (~1,426 input and ~395 output
tokens per image) that is roughly **$0.0003 per image** — about $3 for ten
thousand images.

### It is slower than expected

The binding constraint on the free tier is requests-per-day, not speed. Within
a run, throughput is capped by the token bucket at `--rpm`.

`--workers` beyond 3 or 4 rarely helps: the API rate limit, not your CPU, is
the bottleneck.

### Files with non-ASCII names or titles

Supported throughout, and specifically tested. Filenames, titles, descriptions
and keywords all round-trip Unicode correctly through IPTC and XMP.

If your console shows `caf?` instead of `café`, that is the terminal's display
encoding, not the file. Check with:

```bash
exiftool -charset iptc=UTF8 -json -IPTC:Keywords "path/to/image.jpg"
```

## Registry and recovery

### `.pipeline_registry.json is unreadable`

StockFlow refuses to continue rather than guessing. That is deliberate: an
unreadable registry looks identical to a folder that has never been processed,
and silently assuming the latter would reprocess everything and burn an entire
day of API quota.

If you have a backup (`.pipeline_registry.json.v0.bak` is written before any
migration), restore it. Otherwise:

```bash
stockflow FOLDER --force-fresh-registry
```

The corrupt file is moved aside with a timestamp, never deleted.

### `Recovered from an interrupted run`

Normal after a crash, power cut or forced shutdown mid-move. StockFlow wrote
its intent before moving the file, so on the next run it inspects what is
actually on disk and repairs. The review log records exactly what it did.

If it reports a file in **both** locations, it kept both on purpose and wants
you to decide which to keep.

### I want to reprocess something already handled

```bash
stockflow FOLDER --retry-failed        # retries only ERROR / ERROR_PERMANENT
```

To redo an image that finished successfully, move it back into the folder root
and delete its entry from `.pipeline_registry.json`.

## Results that look wrong

### Good photos are landing in `03_SKIPPED_LOWQUALITY`

Check the score in `Reports/needs_review.txt`. If the model is scoring
consistently low for your subject matter, lower the bar:

```bash
stockflow FOLDER --min-score 45
```

If you enabled `--min-blur`, try without it first. Those thresholds are
starting points derived from the mathematics, not from a labelled dataset —
they are exactly the kind of number that needs tuning to a specific portfolio.

### Sharp photos flagged as soft

The focus score is a high percentile across the frame, so shallow depth of
field should *not* trigger it. If it does, your images may be softer than they
appear at fit-to-screen zoom — check at 100%. Report the measured number from
`report.csv` when comparing.

### Everything is going to `06_REVIEW`

`REVIEW` means the model returned Medium or High rejection risk, or a risk
value that could not be understood. Read `needs_review.txt` for the stated
reason. An unrecognised value routes to REVIEW by design — a risk field that
cannot be parsed must not silently become READY.

### Near-duplicates were not removed

They are never removed. Visually similar shots are logged as `NOTE` lines and
left alone, because two frames from a burst are often both independently worth
submitting. Only **byte-identical** files go to `04_DUPLICATES`.

### The Adobe CSV has a blank category

Expected for Shutterstock categories with no honest Adobe equivalent — "The
Arts", "Education", "Miscellaneous" and "Vintage". Adobe treats Category as
optional and suggests one itself, which beats forcing the image into the wrong
bucket. Each occurrence is logged as a `CHECK` line.

## Getting more detail

```bash
stockflow FOLDER -v
```

And `Reports/stockflow.log` always holds the full debug trace of the last run,
regardless of console verbosity.
