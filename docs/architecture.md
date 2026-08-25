# StockFlow architecture

## The shape of a run

```
folder/
  ├─ scan ─────────► iter_source_files()      non-recursive, honours the registry
  │
  ├─ per image ────► sha256          exact duplicate?  → 04_DUPLICATES
  │                  read_facts      below min MP?     → 02_SKIPPED_LOWRES
  │                  normalize       PNG/HEIC/RAW/oversize → derived JPEG
  │                  pHash           near-duplicate?   → logged, never moved
  │                  analyze_quality focus / noise / exposure, full resolution
  │                  ──► Gemini      title, keywords, category, score, flags
  │                  choose_status   route + reason, produced together
  │                  embed metadata  IPTC + XMP via exiftool
  │                  commit          intent → move → record
  │
  └─ finish ───────► CSVs, reports, registry save, work-dir cleanup
```

## Module map

Four tiers, dependencies point downward only.

**Tier 0 — no internal dependencies**

| Module | Responsibility |
|---|---|
| `errors.py` | Exception hierarchy |
| `models.py` | Frozen dataclasses, `Status` enum, folder names |

**Tier 1 — depends only on tier 0**

| Module | Responsibility |
|---|---|
| `config.py` | `Settings`, precedence resolution, exiftool discovery |
| `limits.py` | Per-model rate limits and self-correction from 429s |
| `rules.py` | All policy. Pure functions, no I/O |
| `marketplaces.py` | Verified Shutterstock and Adobe submission specs |
| `prompt.py` | The vision prompt and its response schema |

**Tier 2 — I/O adapters, none depend on each other**

| Module | Responsibility |
|---|---|
| `imaging/loader.py` | Decoding, ICC→sRGB, EXIF orientation, format probing |
| `imaging/normalize.py` | Conversion, resizing, API encoding |
| `imaging/quality.py` | Local measurement on full-resolution pixels |
| `hashing.py` | SHA-256, pHash, cross-run dedupe index |
| `analyzer.py` | Gemini client, retry, error classification |
| `metadata.py` | exiftool via UTF-8 argfile |
| `registry.py` | Per-folder state, migration, crash recovery |
| `ratelimit.py` | Token bucket, adaptive gate, daily quota |
| `reporting.py` | CSVs and run reports |

**Tier 3 — orchestration**

| Module | Responsibility |
|---|---|
| `pipeline.py` | Sequences everything above |
| `cli.py` | Argument parsing, logging, banner |

`rules.py` is the important seam. Every classification decision lives there as
a pure function, which is why the routing logic can be tested exhaustively
without a filesystem, a network or an API key.

## Design decisions worth knowing

### Two-phase commit on every file move

A move is irreversible from the scanner's point of view. Once a file leaves the
scan root, `iter_source_files` will never see it again. So if the registry
write fails *after* the move, the photo is stranded — sitting in an output
folder, absent from the CSV, invisible to every future run.

The fix is to write the intent before the move:

```
registry.begin(name, action="move", src=..., dest=...)   # flushed to disk
safe_move(src, dest)
registry.commit(name, status, ...)                       # replaces the intent
```

On startup `Registry.reconcile()` inspects any surviving intent and repairs
from what is actually on disk: destination exists and source does not → the
move completed, finish the record. Source exists and destination does not → it
never happened, leave it pending. Both exist → keep both and say so.

### Local quality measured on fixed-size native tiles

Variance-of-Laplacian is strongly scale dependent — measured on the same
photograph it came out about 53× larger at 24MP than at 1024px. Any fixed
threshold is therefore meaningless unless the measurement scale is pinned.

So quality sampling takes fixed-size 256×256 tiles at *native* resolution. A
tile from a 50MP file and a tile from a 12MP file cover the same number of real
sensor pixels, which makes them comparable. It also bounds cost: at most
`GRID × GRID × TILE × TILE` pixels are examined regardless of file size.

The reported focus score is the 90th percentile across tiles, so a sharp
subject on a soft background reads as sharp. Noise is estimated only on the
flattest third of tiles, otherwise foliage and fabric texture read as grain.

### exiftool is driven through a UTF-8 argfile

Not a stylistic choice. Verified against exiftool 13.52, passing tags as
process arguments on Windows produced:

- `café` → `caf?`, `日本の桜` → `????`, with IPTC truncated at the first
  non-ASCII byte — and exiftool still exiting 0, so nothing detected it
- a file *path* containing non-ASCII failing outright with "Invalid filename
  encoding / No matching files"

The argfile fixes encoding, removes the Windows 32KB command-line ceiling that
50 keywords across two tag families approaches, and prevents a model-authored
title beginning with `-` from being read as an option.

Keywords use repeated plain `-IPTC:Keywords=` assignments rather than `+=`.
`+=` appends: running it twice produced `alpha, beta, alpha, gamma`, and a
photographer's existing Lightroom keywords would be carried into commercial
submissions.

### Concurrency is a thread pool, not asyncio

Every stage except the HTTP call is blocking but GIL-releasing: PIL decodes in
C, numpy releases for bulk array operations, hashlib releases for large reads,
`subprocess.run` is an OS wait. A thread pool exploits all of that with no
rewrite. asyncio would only help the one stage that is already the least of the
problem.

Rate limiting is shared across the pool — a single token bucket and a single
adaptive pause. Without that, N workers each discover the same project-wide 429
independently and each backs off privately, so the pool keeps hammering an API
that is already refusing while every worker believes it is being polite.

### Structured output, structured errors

The response schema is sent to the API, so the model's *shape* is enforced
rather than parsed. Values are still coerced defensively — Google's own docs
say to validate, and `"false"`, `"85/100"`, `"HIGH"` and JSON `null` have all
turned up in practice.

429s are classified on `quotaId`, never on `retryDelay`. Google returns a short
retry delay even for per-day exhaustion (34 seconds has been observed), so
honouring it would burn the rest of the day re-hitting the same wall.

## Testing

332 tests, no API key, no network, no real exiftool required.

The three hard seams are faked:

| Seam | Fake |
|---|---|
| Gemini | `FakeAnalyzer` — scripted responses and errors |
| exiftool | `FakeMetadataWriter` — records calls, can simulate failure |
| Filesystem | `tmp_path`, images generated with Pillow |

Tests that genuinely need the exiftool binary are marked `integration` and skip
themselves when it is absent.

One lesson is baked into the suite: **fakes cannot catch a malformed API
schema.** A response schema containing an empty enum value passed all 327
fake-backed tests and failed every single real call. `TestResponseSchema`
exists specifically to close that class of gap.
