# StockFlow batch — instructions for Claude Code

This folder is a **StockFlow batch**: stock photos waiting to be described, sorted
and exported for Shutterstock and Adobe Stock. You (Claude) are the vision step.
You look at each photo and write its metadata; StockFlow does everything else —
duplicates, quality measurement, sorting into folders, embedded IPTC/XMP, and
both marketplace CSVs.

Read this whole file before touching anything.

## Ground rules

- **Never delete, rename or edit a photo.** StockFlow moves them; you don't.
- **Make no Gemini/API calls.** Always run StockFlow with `--provider sidecar`.
- **Work only inside this folder.** Nothing here gets committed or uploaded.
- **Write each photo's JSON as soon as you've done that photo**, so progress
  survives if the session ends.
- If something is unclear (a subject you can't identify, a person, a brand),
  say so in the metadata and flag it — don't guess.

## Step 1 — see what's waiting

```bash
python -m stockflow "<THIS FOLDER>" --prepare-sidecars
```

(`<THIS FOLDER>` is the folder containing this file. StockFlow must be
installed: `pip install -e <path to StockFlow>`.)

This lists the photos without metadata in `metadata/_PENDING.txt`, and writes two
images per photo into `metadata/_previews/`:

- `<name>.preview.jpg` — the whole photo at 1024 px, what a vision model sees
- `<name>.corners.jpg` — the four corners at **full resolution**, 2×2 grid
  (top-left, top-right / bottom-left, bottom-right)

If it says 0 pending but photos are still in the folder, run it again with
`--retry-failed` added, which reconsiders photos an earlier run marked as errors.

## Step 2 — load the rules StockFlow's vision prompt uses

Follow exactly the same rules Gemini is given:

```bash
python -c "from stockflow.prompt import TASK_PROMPT; print(TASK_PROMPT)"
python -c "from stockflow.rules import SHUTTERSTOCK_CATEGORIES; print(SHUTTERSTOCK_CATEGORIES)"
```

## Step 3 — describe each photo

Work through `_PENDING.txt` in order, **at most ~20 photos per session**. Images
use a lot of context; when you reach the limit, stop and tell the user how many
remain so they can open a fresh session here.

For each photo:

1. Look at `metadata/_previews/<name>.preview.jpg`.
2. Look at `metadata/_previews/<name>.corners.jpg`. **Any** text, signature,
   logo, date/time stamp or camera-model stamp in a corner means
   `watermark_or_overlay_visible: true`, and quote it in `rejection_reason`.
3. Write `metadata/<name>.json` (the full filename including its extension,
   plus `.json`, e.g. `metadata/IMG_0421.jpg.json`):

```json
{
  "title": "8-18 words, factual, what a buyer would search for first",
  "description": "One or two plain sentences: subject, setting, likely use.",
  "keywords": ["40 to 50 terms", "most important first", "no commas inside a keyword"],
  "category": "one value from SHUTTERSTOCK_CATEGORIES",
  "category2": "optional second category, or omit the key",
  "commercial_score": 0,
  "rejection_risk": "Low | Medium | High",
  "rejection_reason": "one specific sentence when Medium or High, else empty",
  "people_visible": false,
  "property_or_trademark_visible": false,
  "watermark_or_overlay_visible": false
}
```

Extra rules on top of the prompt:

- **Screenshots, app screens, collages, scans** are not photographs:
  `watermark_or_overlay_visible: true`, `rejection_risk: "High"`,
  `commercial_score` under 10, and say why in `rejection_reason`.
- **Species only when certain.** "Jumping spider" is fine if the eyes show it;
  a Latin name needs certainty. Spiders are arachnids, not insects.
- **Never invent** a location, date, event, brand, or person's identity.
- **Resolution:** below 4 MP, Shutterstock rejects the file; set
  `rejection_risk` to at least Medium and say so.

## Step 4 — check the JSON before running

```bash
python -c "import json,pathlib; from stockflow.analyzer import parse_analysis; [print(p.name, 'OK', len(parse_analysis(json.loads(p.read_text(encoding='utf-8'))).keywords), 'keywords') for p in sorted(pathlib.Path('metadata').glob('*.json'))]"
```

Run it from inside this folder. Fix anything that errors.

## Step 5 — run StockFlow

```bash
python -m stockflow "<THIS FOLDER>" --provider sidecar
```

Add `--min-megapixels N` only if the user asks. Photos without metadata stay
where they are, marked pending — nothing is lost.

## Step 6 — report back

Tell the user, briefly:

- how many photos went to each folder (`01_READY_UPLOAD`, `06_REVIEW`, …)
- every photo not ready, with its reason
- anything you were unsure about
- where the CSVs are: `Reports/shutterstock_upload.csv`, `Reports/adobe_stock_upload.csv`
- how many photos are still pending

Don't upload anything to a marketplace. The user reviews and uploads.
