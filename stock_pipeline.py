"""
========================================================
  STOCK METADATA PIPELINE  —  v2 (fixed + sell-optimized)
  Free. Local. No subscriptions.
========================================================

WHAT THIS DOES:
  Looks at each photo with AI, writes a SALES-OPTIMIZED title +
  description + keywords INSIDE the file, skips weak/blurry/too-small
  shots, flags photos that need a model/property release, and makes
  a CSV so Shutterstock auto-fills on upload. Adobe Stock also reads
  the embedded metadata automatically.

WHAT'S NEW IN v2 (vs. the original script):
  - Fixed: the model name ("gemini-2.0-flash") was retired by Google
    on June 1, 2026 and would fail on every photo. Now uses
    "gemini-2.5-flash" (currently free, check SETTINGS below).
  - Fixed: failed photos used to be permanently skipped forever, even
    for a one-off rate-limit hiccup. Now errors retry automatically
    (in-run backoff + up to 3 separate runs) before being logged as
    permanent and left for you to review.
  - Fixed: exiftool could fail to be found depending on which folder
    you ran the script from. Now it's located relative to this
    script and checked BEFORE any API calls are spent.
  - New: rewrote the AI prompt around real microstock SEO practice —
    keyword layering (subject -> concept -> use-case -> visual
    descriptor), front-loaded titles, and a second optional category
    (Shutterstock allows up to 2).
  - New: flags photos that show a recognizable person or branded/
    private property, so you know which ones need a release before
    you upload (a common reason first submissions get rejected).
  - New: gives a one-line, specific reason WHY a photo is likely to
    be rejected (not just a risk score), so you can actually learn
    what to shoot/avoid next time.
  - New: checks resolution (Shutterstock requires 4MP+) BEFORE
    spending an API call on a photo that can't be accepted anyway.
  - New: flags oversized files and PNGs (Shutterstock wants JPEG/TIFF)
    in the review log so you're not surprised at upload time.

========================================================
  ONE-TIME SETUP  (15 minutes, do this once)
========================================================

STEP 1 — Get your free Gemini API key
  Go to: https://aistudio.google.com
  Click "Get API key" -> "Create API key"
  Copy the key (looks like: AIzaSy...)

STEP 2 — Save the key on your PC (do NOT paste it into this file)
  Open PowerShell and run:
    setx GEMINI_API_KEY "paste-your-key-here"
  Then CLOSE and REOPEN PowerShell. That's it.

STEP 3 — Install Python packages
  pip install google-genai pillow

STEP 4 — Install ExifTool (writes metadata into your photo files)
  Go to: https://exiftool.org
  Download "Windows Executable"
  Unzip it -> rename the .exe to exactly:  exiftool.exe
  Put exiftool.exe in the SAME FOLDER as this script.

NOTE on the AI model: Google retires Gemini model names every few
months. This script currently uses "gemini-2.5-flash" (free tier as
of mid-2026). If you ever see errors mentioning "model not found" or
"404", open this file, find MODEL = ... near the top, and check
https://ai.google.dev/gemini-api/docs/deprecations for the current
free Flash model name to swap in.

========================================================
  HOW TO RUN IT
========================================================

  python stock_pipeline_v2.py "D:\\Photos\\batch_01"

  Replace the path with your actual folder of photos.
  It will create two files inside that folder:
    - shutterstock_upload.csv   (import this on Shutterstock)
    - needs_review.txt          (skips, errors, AND release/rejection
                                  warnings worth reading before upload)

  Safe to stop and restart anytime. A photo that errors out gets
  retried automatically on the next run (up to 3 attempts total)
  before it's logged as a permanent failure for you to check by hand.

========================================================
  AFTER RUNNING — UPLOAD IN 10 MINUTES
========================================================

  Before you upload, skim needs_review.txt for any lines starting
  with FLAG (release may be needed) or RISK (likely rejection reason)
  — fixing or dropping those photos now saves a rejection later.

  SHUTTERSTOCK:
  1. Go to submit.shutterstock.com
  2. Drag your entire batch folder in
  3. Click the CSV button -> upload shutterstock_upload.csv
  4. Everything fills in -> hit Submit. Done.

  ADOBE STOCK:
  1. Go to contributor.stock.adobe.com
  2. Drag the same folder in
  3. Fields auto-fill from the file metadata -> hit Submit. Done.

"""

import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image
import io

# ──────────────────────────── SETTINGS ────────────────────────────────────
SCRIPT_DIR       = Path(__file__).resolve().parent
MODEL            = "gemini-2.5-flash"   # free tier as of mid-2026 — see NOTE above
BATCH_LIMIT      = 50     # photos per run (start small, raise later)
PAUSE_SECONDS    = 4      # pause between API calls (free tier ~15 requests/min)
MIN_SCORE        = 60     # skip photos the AI scores below this (0-100)
MAX_PIXELS       = 1024   # resize before sending to API (doesn't touch original)
MIN_MEGAPIXELS   = 4.0    # Shutterstock's minimum — skip smaller photos, no API call spent
MAX_FILE_SIZE_MB = 50     # Shutterstock's max upload size — just a warning, not a block
MAX_ATTEMPTS     = 3      # retry a failing photo this many separate RUNS before giving up
IMAGE_TYPES      = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def find_exiftool() -> str:
    """Look for exiftool next to this script first, then fall back to PATH."""
    candidate = SCRIPT_DIR / ("exiftool.exe" if os.name == "nt" else "exiftool")
    if candidate.exists():
        return str(candidate)
    return "exiftool.exe" if os.name == "nt" else "exiftool"


EXIFTOOL = find_exiftool()

SHUTTERSTOCK_CATEGORIES = {
    "Abstract", "Animals/Wildlife", "The Arts", "Backgrounds/Textures",
    "Beauty/Fashion", "Buildings/Landmarks", "Business/Finance", "Celebrities",
    "Education", "Food and Drink", "Healthcare/Medical", "Holidays",
    "Industrial", "Interiors", "Miscellaneous", "Nature", "Objects",
    "Parks/Outdoor", "People", "Religion", "Science", "Signs/Symbols",
    "Sports/Recreation", "Technology", "Transportation", "Vintage",
}
# ^ double-check this against the live category list on
#   submit.shutterstock.com if anything seems off — agencies do
#   occasionally add/rename categories.

# ──────────────────────────── THE PROMPT ──────────────────────────────────
PROMPT = """You are a senior commercial stock photo editor and SEO copywriter
working for contributors selling on Shutterstock, Adobe Stock, and similar
microstock marketplaces. Buyers find images almost entirely through search,
so the title and keywords must be written in language buyers actually type —
not artistic or poetic language.

Analyze the attached image and return ONLY valid JSON with exactly these keys
(no markdown, no commentary, no backticks):

{
  "title": "",
  "description": "",
  "keywords": [],
  "category": "",
  "category2": "",
  "commercial_score": 0,
  "rejection_risk": "",
  "rejection_reason": "",
  "people_visible": false,
  "property_or_trademark_visible": false
}

TITLE — this also becomes the Shutterstock "Description" field, which
functions like a searchable headline, not a caption:
- Put the single most-searched subject/concept in the first few words.
- Strictly factual: describe what is literally visible. Never invent a
  backstory, location, or emotion that isn't visually evident.
- 8-18 words, under 180 characters. No camera/lens jargon ("shot on",
  "bokeh", "f/2.8"). No filler like "stock photo of" or "image showing".
- Example of the right register: "Young woman drinking coffee at laptop in
  sunlit home office" — not "A nice photo of someone working."

DESCRIPTION — used only in the embedded file metadata (read by Adobe Stock):
- 1-2 plain sentences. Concrete subject + setting + likely use, in your own
  words. Don't just repeat the title — add the detail it didn't have room for.

KEYWORDS — 40 to 50 terms, ordered MOST to LEAST important. This drives
discoverability more than anything else on this list, so take it seriously:
- Use singular nouns ("mountain" not "mountains") unless the plural is the
  natural search term.
- Layer 1 (~15 terms): literal subjects, objects, and setting visible in
  the frame.
- Layer 2 (~10 terms): concepts/emotions buyers search by (e.g. teamwork,
  freedom, mindfulness, growth, isolation, celebration) — ONLY if visually
  justified by composition, expression, or context. Never invented.
- Layer 3 (~10 terms): commercial use-case terms (e.g. copy space,
  background, banner, advertising, website, blog, presentation, lifestyle,
  wellness, technology).
- Layer 4 (remaining): visual descriptors that affect search — dominant
  colors, lighting (golden hour, backlit, natural light), composition
  (close-up, top view, negative space), season, time of day.
- Never invent a location, brand name, trademark, or named landmark you
  cannot positively confirm. No keyword stuffing — every term must
  genuinely apply to this image.

CATEGORY / CATEGORY2:
- "category" is REQUIRED — pick exactly ONE from this list (copy exactly):
  Abstract, Animals/Wildlife, The Arts, Backgrounds/Textures, Beauty/Fashion,
  Buildings/Landmarks, Business/Finance, Celebrities, Education, Food and
  Drink, Healthcare/Medical, Holidays, Industrial, Interiors, Miscellaneous,
  Nature, Objects, Parks/Outdoor, People, Religion, Science, Signs/Symbols,
  Sports/Recreation, Technology, Transportation, Vintage
- "category2" is OPTIONAL — fill it ONLY if a second category from the same
  list is clearly also applicable (Shutterstock allows up to 2 and a good
  second category genuinely increases discoverability). Otherwise return "".

COMMERCIAL_SCORE: integer 0-100. How sellable is THIS specific frame today —
weigh sharpness/focus, lighting, composition and negative space, how
oversaturated this subject already is on stock sites, and whether there's
an obvious buyer (advertising, editorial, blog, web design, print).

REJECTION_RISK: exactly one of — Low / Medium / High — your honest estimate
of the odds a human reviewer rejects this for quality or commercial-value
reasons.

REJECTION_REASON: if rejection_risk is Medium or High, ONE short, specific,
actionable sentence on what's wrong (e.g. "Slightly soft focus on the main
subject" or "Subject is extremely common on stock sites without a unique
angle"). If rejection_risk is Low, return "".

PEOPLE_VISIBLE: true if any recognizable human face or person appears in
the frame, even partially — the contributor needs a signed model release to
sell this commercially, so flag honestly rather than guessing low.

PROPERTY_OR_TRADEMARK_VISIBLE: true if a recognizable logo, branded product,
artwork, or distinctive private property/architecture appears that would
need a property release or could cause a rejection.

Return JSON only."""

# ──────────────────────────── REGISTRY HELPERS ────────────────────────────

def load_registry(folder: Path) -> dict:
    p = folder / ".pipeline_registry.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    norm = {}
    for name, value in raw.items():
        if isinstance(value, str):
            # legacy format from the original script — give old "error"
            # entries a fresh chance to retry instead of blacklisting forever
            norm[name] = {"status": value, "attempts": 0 if value == "error" else 1}
        else:
            norm[name] = value
    return norm


def save_registry(folder: Path, reg: dict):
    p = folder / ".pipeline_registry.json"
    p.write_text(json.dumps(reg, indent=2))


def is_pending(name: str, registry: dict) -> bool:
    entry = registry.get(name)
    if entry is None:
        return True
    status = entry.get("status")
    if status in ("done", "skipped", "skipped_lowres", "error_permanent"):
        return False
    if status == "error":
        return entry.get("attempts", 0) < MAX_ATTEMPTS
    return True

# ──────────────────────────── FUNCTIONS ───────────────────────────────────

def check_exiftool() -> bool:
    try:
        subprocess.run([EXIFTOOL, "-ver"], capture_output=True, text=True, check=True)
        return True
    except Exception:
        return False


def resize_for_api(path: Path) -> bytes:
    img = Image.open(path).convert("RGB")
    img.thumbnail((MAX_PIXELS, MAX_PIXELS), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def extract_json_text(text: str) -> str:
    """Defensively strip ```json fences if the model ever adds them."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def validate_response(data: dict):
    required = ["title", "description", "keywords", "category",
                "commercial_score", "rejection_risk"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Model response missing keys: {missing}")
    if not isinstance(data["keywords"], list) or not data["keywords"]:
        raise ValueError("Model returned no usable keywords")


def call_gemini(client, image_bytes: bytes, max_retries: int = 3) -> dict:
    """Calls Gemini with automatic backoff-retry for transient errors."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    PROMPT,
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            raw = extract_json_text(resp.text)
            data = json.loads(raw)
            validate_response(data)
            data.setdefault("category2", "")
            data.setdefault("rejection_reason", "")
            data.setdefault("people_visible", False)
            data.setdefault("property_or_trademark_visible", False)
            return data
        except Exception as e:
            last_err = e
            if attempt == max_retries:
                break
            msg = str(e).lower()
            is_rate_limit = any(t in msg for t in ("429", "resource_exhausted", "rate limit", "quota"))
            wait = 15 * attempt if is_rate_limit else 3 * attempt
            print(f" retry {attempt}/{max_retries} in {wait}s ({e})", end=" ... ", flush=True)
            time.sleep(wait)
    raise last_err


def embed_metadata(path: Path, title: str, description: str, keywords: list):
    kw_args = []
    for kw in keywords:
        kw_args += [f"-Keywords+={kw}", f"-XMP-dc:Subject+={kw}"]
    cmd = [
        EXIFTOOL, "-overwrite_original",
        f"-Title={title}",
        f"-Description={description}",
        f"-Caption-Abstract={description}",
        f"-XMP-dc:Title={title}",
        f"-XMP-dc:Description={description}",
        *kw_args,
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"exiftool failed: {result.stderr.strip()}")


def write_csv_row(csv_path: Path, filename: str, title: str, keywords: list,
                   category: str, category2: str):
    categories = ", ".join(c for c in (category, category2) if c)
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["Filename", "Description", "Keywords", "Categories"])
        # Shutterstock's "Description" column is the searchable title field,
        # not a long caption — this matches their own contributor docs.
        w.writerow([filename, title, ", ".join(keywords), categories])


def log(review_log: Path, line: str):
    with open(review_log, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")

# ──────────────────────────── MAIN ────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print('Usage:  python stock_pipeline_v2.py "D:\\Photos\\batch_01"')
        sys.exit(1)

    folder = Path(sys.argv[1]).expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        sys.exit(1)

    if not check_exiftool():
        print(f"\nCan't run exiftool ({EXIFTOOL}).")
        print("Put exiftool.exe in the same folder as this script,")
        print("or make sure 'exiftool' is on your system PATH, then try again.\n")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\nGEMINI_API_KEY not found.")
        print('Run this in PowerShell:  setx GEMINI_API_KEY "your-key-here"')
        print("Then close and reopen PowerShell, and try again.\n")
        sys.exit(1)

    client     = genai.Client(api_key=api_key)
    registry   = load_registry(folder)
    csv_path   = folder / "shutterstock_upload.csv"
    review_log = folder / "needs_review.txt"

    all_images = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in IMAGE_TYPES and is_pending(p.name, registry)
    )

    if not all_images:
        print("Nothing left to process in this folder (or everything already done/skipped).")
        return

    batch = all_images[:BATCH_LIMIT]
    print(f"\nFolder: {folder}")
    print(f"Pending: {len(all_images)} images. Processing {len(batch)} this run.")
    print(f"Skipping anything scored below {MIN_SCORE}/100 or under {MIN_MEGAPIXELS}MP.\n")

    uploaded = 0
    skipped  = 0
    flagged  = 0

    for i, path in enumerate(batch, 1):
        print(f"[{i}/{len(batch)}] {path.name}", end=" ... ", flush=True)

        # ── pre-flight notes that don't block processing ──
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            log(review_log, f"NOTE  {path.name}  file is {size_mb:.0f}MB, "
                             f"Shutterstock's max is {MAX_FILE_SIZE_MB}MB — compress before upload")
        if path.suffix.lower() == ".png":
            log(review_log, f"NOTE  {path.name}  PNG — Shutterstock photo uploads need JPEG/TIFF "
                             f"(Adobe Stock accepts PNG)")

        try:
            # ── resolution pre-check — skip BEFORE spending an API call ──
            with Image.open(path) as probe:
                mp = (probe.size[0] * probe.size[1]) / 1_000_000
            if mp < MIN_MEGAPIXELS:
                print(f"SKIPPED (too small: {mp:.1f}MP, need {MIN_MEGAPIXELS}MP+)")
                log(review_log, f"SKIPPED  {path.name}  resolution={mp:.1f}MP (need {MIN_MEGAPIXELS}MP+)")
                registry[path.name] = {"status": "skipped_lowres", "attempts": 0}
                save_registry(folder, registry)
                skipped += 1
                continue

            img_bytes = resize_for_api(path)
            data      = call_gemini(client, img_bytes)

            score = int(data.get("commercial_score", 0))
            risk  = data.get("rejection_risk", "Unknown")
            reason = data.get("rejection_reason", "")

            if risk in ("Medium", "High") and reason:
                log(review_log, f"RISK={risk}  {path.name}  {reason}")

            if score < MIN_SCORE:
                print(f"SKIPPED (score {score}/100, risk: {risk})")
                log(review_log, f"SKIPPED  {path.name}  score={score}  risk={risk}")
                registry[path.name] = {"status": "skipped", "attempts": 0}
                save_registry(folder, registry)
                skipped += 1
                time.sleep(PAUSE_SECONDS)
                continue

            title       = data["title"].strip()
            description = data["description"].strip()
            keywords    = [k.strip() for k in data["keywords"] if k.strip()]
            category    = data.get("category", "").strip()
            category2   = data.get("category2", "").strip()

            if category not in SHUTTERSTOCK_CATEGORIES:
                log(review_log, f"BAD CATEGORY  {path.name}  got='{category}'")
                category = "Miscellaneous"
            if category2 and (category2 not in SHUTTERSTOCK_CATEGORIES or category2 == category):
                category2 = ""

            embed_metadata(path, title, description, keywords)
            write_csv_row(csv_path, path.name, title, keywords, category, category2)

            registry[path.name] = {"status": "done", "attempts": 0}
            save_registry(folder, registry)
            uploaded += 1

            flags = []
            if data.get("people_visible"):
                flags.append("PEOPLE — model release needed")
            if data.get("property_or_trademark_visible"):
                flags.append("PROPERTY/TRADEMARK — release may be needed")
            if flags:
                flagged += 1
                log(review_log, f"FLAG  {path.name}  " + " & ".join(flags))

            flag_str = f"  [!] {' & '.join(flags)}" if flags else ""
            print(f"OK  score={score}/100  risk={risk}  keywords={len(keywords)}{flag_str}")

        except Exception as e:
            attempts = registry.get(path.name, {}).get("attempts", 0) + 1
            status = "error" if attempts < MAX_ATTEMPTS else "error_permanent"
            print(f"ERROR (attempt {attempts}/{MAX_ATTEMPTS}) — {e}")
            log(review_log, f"ERROR  {path.name}  attempt={attempts}  {e}")
            registry[path.name] = {"status": status, "attempts": attempts, "last_error": str(e)[:300]}
            save_registry(folder, registry)

        time.sleep(PAUSE_SECONDS)

    remaining = len(all_images) - len(batch)
    print(f"\n── Done ─────────────────────────────────────────")
    print(f"   Processed this run    : {len(batch)}")
    print(f"   Ready to upload       : {uploaded}")
    print(f"   Skipped (score/size)  : {skipped}")
    print(f"   Flagged (need release): {flagged}")
    print(f"   Still pending         : {remaining} (run again to continue)")
    print(f"   CSV ready at          : {csv_path}")
    if review_log.exists():
        print(f"   Review log            : {review_log}  <- check this before uploading")
    print()


if __name__ == "__main__":
    main()
