"""
========================================================
  STOCK METADATA PIPELINE  —  Final Version
  Free. Local. No subscriptions.
========================================================

WHAT THIS DOES:
  Looks at each photo with AI, writes a title + description +
  keywords INSIDE the file, skips weak/blurry shots, and makes
  a CSV so Shutterstock auto-fills on upload. Adobe Stock also
  reads the embedded metadata automatically.

========================================================
  ONE-TIME SETUP  (15 minutes, do this once)
========================================================

STEP 1 — Get your free Gemini API key
  Go to: https://aistudio.google.com
  Click "Get API key" → "Create API key"
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
  Unzip it → rename the .exe to exactly:  exiftool.exe
  Put exiftool.exe in the SAME FOLDER as this script.

========================================================
  HOW TO RUN IT
========================================================

  python stock_pipeline_final.py "D:\Photos\batch_01"

  Replace the path with your actual folder of photos.
  It will create two files inside that folder:
    - shutterstock_upload.csv   (import this on Shutterstock)
    - needs_review.txt          (photos it skipped or had trouble with)

  Safe to stop and restart anytime — it remembers what it already did.

========================================================
  AFTER RUNNING — UPLOAD IN 10 MINUTES
========================================================

  SHUTTERSTOCK:
  1. Go to submit.shutterstock.com
  2. Drag your entire batch folder in
  3. Click the CSV button → upload shutterstock_upload.csv
  4. Everything fills in → hit Submit. Done.

  ADOBE STOCK:
  1. Go to contributor.stock.adobe.com
  2. Drag the same folder in
  3. Fields auto-fill from the file metadata → hit Submit. Done.

"""

import base64
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image, ImageFilter
import io

# ──────────────────────────── SETTINGS ────────────────────────────────────
MODEL            = "gemini-2.0-flash"   # free tier model
BATCH_LIMIT      = 50    # photos per run (start small, raise later)
PAUSE_SECONDS    = 4     # pause between API calls (avoids rate limits)
MIN_SCORE        = 60    # skip photos the AI scores below this (0-100)
MAX_PIXELS       = 1024  # resize before sending to API (doesn't touch original)
EXIFTOOL         = "exiftool.exe" if os.name == "nt" else "exiftool"
IMAGE_TYPES      = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

SHUTTERSTOCK_CATEGORIES = {
    "Abstract", "Animals/Wildlife", "The Arts", "Backgrounds/Textures",
    "Beauty/Fashion", "Buildings/Landmarks", "Business/Finance", "Celebrities",
    "Education", "Food and Drink", "Healthcare/Medical", "Holidays",
    "Industrial", "Interiors", "Miscellaneous", "Nature", "Objects",
    "Parks/Outdoor", "People", "Religion", "Science", "Signs/Symbols",
    "Sports/Recreation", "Technology", "Transportation", "Vintage",
}

# ──────────────────────────── THE PROMPT ──────────────────────────────────
PROMPT = """You are a professional stock photography editor.

Analyze this image for commercial stock photography websites such as
Shutterstock, Adobe Stock, and Alamy.

Return ONLY valid JSON with exactly these keys:

{
  "title": "",
  "description": "",
  "keywords": [],
  "category": "",
  "commercial_score": 0,
  "rejection_risk": ""
}

Requirements:
1. Title: factual, commercially useful, under 120 characters. No camera jargon.
2. Description: 1-2 sentences describing the image for a stock buyer. Clear and direct.
3. Keywords: 40-50 terms ordered by importance.
   - Include literal subjects visible in the image.
   - Include commercial concepts buyers search for.
   - Include concepts like background, copy space, ecology, growth,
     sustainability, wildlife, conservation, freshness, mindfulness,
     environment ONLY when visually justified.
   - No keyword stuffing. No trademarks. No brand names. No hallucinated locations.
4. Category: pick exactly ONE from this list (copy it exactly):
   Abstract, Animals/Wildlife, The Arts, Backgrounds/Textures, Beauty/Fashion,
   Buildings/Landmarks, Business/Finance, Celebrities, Education, Food and Drink,
   Healthcare/Medical, Holidays, Industrial, Interiors, Miscellaneous, Nature,
   Objects, Parks/Outdoor, People, Religion, Science, Signs/Symbols,
   Sports/Recreation, Technology, Transportation, Vintage
5. commercial_score: integer 0-100. Rate how sellable this image is on stock sites.
   Consider: sharpness, lighting, composition, subject demand, clutter, copy space.
6. rejection_risk: exactly one of — Low / Medium / High

Return JSON only. No explanation. No markdown. No backticks."""

# ──────────────────────────── FUNCTIONS ───────────────────────────────────

def load_registry(folder: Path) -> dict:
    p = folder / ".pipeline_registry.json"
    return json.loads(p.read_text()) if p.exists() else {}

def save_registry(folder: Path, reg: dict):
    p = folder / ".pipeline_registry.json"
    p.write_text(json.dumps(reg, indent=2))

def resize_for_api(path: Path) -> bytes:
    img = Image.open(path).convert("RGB")
    img.thumbnail((MAX_PIXELS, MAX_PIXELS), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()

def call_gemini(client, image_bytes: bytes) -> dict:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            PROMPT,
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    raw = resp.text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)

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
        print(f"   [exiftool] {result.stderr.strip()}")

def write_csv_row(csv_path: Path, filename, title, description, keywords, category):
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["Filename", "Description", "Keywords", "Categories"])
        w.writerow([filename, title, ", ".join(keywords), category])

# ──────────────────────────── MAIN ────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print('Usage:  python stock_pipeline_final.py "D:\\Photos\\batch_01"')
        sys.exit(1)

    folder = Path(sys.argv[1]).expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\nGEMINI_API_KEY not found.")
        print("Run this in PowerShell:  setx GEMINI_API_KEY \"your-key-here\"")
        print("Then close and reopen PowerShell, and try again.\n")
        sys.exit(1)

    client     = genai.Client(api_key=api_key)
    registry   = load_registry(folder)
    csv_path   = folder / "shutterstock_upload.csv"
    review_log = folder / "needs_review.txt"

    all_images = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in IMAGE_TYPES and p.name not in registry
    )

    if not all_images:
        print("All images in this folder are already processed. Point to a new folder.")
        return

    batch = all_images[:BATCH_LIMIT]
    print(f"\nFolder: {folder}")
    print(f"Unprocessed: {len(all_images)} images. Processing {len(batch)} this run.")
    print(f"Skipping anything scored below {MIN_SCORE}/100.\n")

    uploaded = 0
    skipped  = 0

    for i, path in enumerate(batch, 1):
        print(f"[{i}/{len(batch)}] {path.name}", end=" ... ", flush=True)
        try:
            img_bytes = resize_for_api(path)
            data      = call_gemini(client, img_bytes)

            score = int(data.get("commercial_score", 0))
            risk  = data.get("rejection_risk", "Unknown")

            if score < MIN_SCORE:
                print(f"SKIPPED (score {score}/100, risk: {risk})")
                with open(review_log, "a") as f:
                    f.write(f"SKIPPED  {path.name}  score={score}  risk={risk}\n")
                registry[path.name] = "skipped"
                save_registry(folder, registry)
                skipped += 1
                time.sleep(PAUSE_SECONDS)
                continue

            title       = data["title"].strip()
            description = data["description"].strip()
            keywords    = [k.strip() for k in data["keywords"] if k.strip()]
            category    = data.get("category", "").strip()

            if category not in SHUTTERSTOCK_CATEGORIES:
                with open(review_log, "a") as f:
                    f.write(f"BAD CATEGORY  {path.name}  got='{category}'\n")
                category = "Miscellaneous"

            embed_metadata(path, title, description, keywords)
            write_csv_row(csv_path, path.name, title, description, keywords, category)

            registry[path.name] = "done"
            save_registry(folder, registry)
            uploaded += 1
            print(f"OK  score={score}/100  risk={risk}  keywords={len(keywords)}")

        except Exception as e:
            print(f"ERROR — {e}")
            with open(review_log, "a") as f:
                f.write(f"ERROR  {path.name}  {e}\n")
            registry[path.name] = "error"
            save_registry(folder, registry)

        time.sleep(PAUSE_SECONDS)

    remaining = len(all_images) - len(batch)
    print(f"\n── Done ─────────────────────────────────────────")
    print(f"   Processed this run : {len(batch)}")
    print(f"   Ready to upload    : {uploaded}")
    print(f"   Skipped (low score): {skipped}")
    print(f"   Still in folder    : {remaining} (run again to continue)")
    print(f"   CSV ready at       : {csv_path}")
    if review_log.exists():
        print(f"   Review log         : {review_log}")
    print()

if __name__ == "__main__":
    main()
