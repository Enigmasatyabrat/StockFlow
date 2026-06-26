
"""
StockFlow v4
=============
AI-powered workflow for preparing stock photography for Shutterstock,
Adobe Stock, and other stock marketplaces.

v4 focus:
- automatic folder organization
- smart rename of best assets
- low-res / low-quality / release-needed / duplicate separation
- ready-to-upload folder for the best images only
- report generation

Author: Satyabrat Mishra
License: Apache-2.0
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from google import genai
from google.genai import types
from PIL import Image, ImageFilter

# ──────────────────────────── SETTINGS ────────────────────────────────────
SCRIPT_DIR       = Path(__file__).resolve().parent
MODEL            = "gemini-2.5-flash"
BATCH_LIMIT      = 50
PAUSE_SECONDS    = 4
MIN_SCORE        = 60
MAX_PIXELS       = 1024
MIN_MEGAPIXELS   = 4.0
MAX_FILE_SIZE_MB = 50
MAX_ATTEMPTS     = 3
IMAGE_TYPES      = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# Output folders
FOLDER_SOURCE_ORIGINALS = "00_SOURCE_ORIGINALS"
FOLDER_READY            = "01_READY_UPLOAD"
FOLDER_LOWRES           = "02_SKIPPED_LOWRES"
FOLDER_LOWQUALITY       = "03_SKIPPED_LOWQUALITY"
FOLDER_DUPLICATES       = "04_DUPLICATES"
FOLDER_NEEDS_RELEASE    = "05_NEEDS_RELEASE"
FOLDER_REVIEW           = "06_REVIEW"
FOLDER_REPORTS          = "Reports"

DESTINATION_FOLDERS = [
    FOLDER_SOURCE_ORIGINALS,
    FOLDER_READY,
    FOLDER_LOWRES,
    FOLDER_LOWQUALITY,
    FOLDER_DUPLICATES,
    FOLDER_NEEDS_RELEASE,
    FOLDER_REVIEW,
    FOLDER_REPORTS,
]

STATUS_READY = "READY"
STATUS_LOWRES = "LOW_RESOLUTION"
STATUS_LOWQUALITY = "LOW_QUALITY"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_NEEDS_RELEASE = "NEEDS_RELEASE"
STATUS_REVIEW = "REVIEW"
STATUS_ERROR = "ERROR"
STATUS_ERROR_PERMANENT = "ERROR_PERMANENT"

# ──────────────────────────── HELPERS ─────────────────────────────────────

def find_exiftool() -> str:
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

DESCRIPTION — used only in the embedded file metadata:
- 1-2 plain sentences. Concrete subject + setting + likely use, in your own
  words.

KEYWORDS — 40 to 50 terms, ordered MOST to LEAST important:
- Layer literal subjects, concepts, use-cases, and visual descriptors.
- Never invent a location, brand name, trademark, or named landmark.
- No keyword stuffing.

CATEGORY / CATEGORY2:
- "category" is REQUIRED — pick exactly ONE from this list:
  Abstract, Animals/Wildlife, The Arts, Backgrounds/Textures, Beauty/Fashion,
  Buildings/Landmarks, Business/Finance, Celebrities, Education, Food and Drink,
  Healthcare/Medical, Holidays, Industrial, Interiors, Miscellaneous, Nature,
  Objects, Parks/Outdoor, People, Religion, Science, Signs/Symbols,
  Sports/Recreation, Technology, Transportation, Vintage
- "category2" is OPTIONAL — fill it only if clearly applicable.

COMMERCIAL_SCORE: integer 0-100. How sellable is THIS specific frame today.

REJECTION_RISK: exactly one of — Low / Medium / High.

REJECTION_REASON: if risk is Medium or High, one short actionable sentence.
If Low, return "".

PEOPLE_VISIBLE: true if any recognizable human face or person appears in
the frame, even partially.

PROPERTY_OR_TRADEMARK_VISIBLE: true if a recognizable logo, branded product,
artwork, or distinctive private property/architecture appears that would
need a release or could cause a rejection.

Return JSON only."""

# ──────────────────────────── FILE SYSTEM / REGISTRY ──────────────────────

def load_registry(folder: Path) -> dict:
    path = folder / ".pipeline_registry.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    normalized = {}
    for name, value in raw.items():
        if isinstance(value, str):
            normalized[name] = {"status": value, "attempts": 0 if value == "error" else 1}
        else:
            normalized[name] = value
    return normalized


def save_registry(folder: Path, registry: dict):
    path = folder / ".pipeline_registry.json"
    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_registry(folder: Path, registry: dict, names: Iterable[str], status: str, **extra):
    for name in names:
        registry[name] = {"status": status, **extra}
    save_registry(folder, registry)


def is_pending(name: str, registry: dict) -> bool:
    entry = registry.get(name)
    if entry is None:
        return True
    status = entry.get("status")
    if status in {STATUS_READY, STATUS_LOWRES, STATUS_LOWQUALITY, STATUS_DUPLICATE, STATUS_NEEDS_RELEASE, STATUS_REVIEW, STATUS_ERROR_PERMANENT}:
        return False
    if status == STATUS_ERROR:
        return entry.get("attempts", 0) < MAX_ATTEMPTS
    return True


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def ensure_output_dirs(folder: Path):
    for name in DESTINATION_FOLDERS:
        ensure_dir(folder / name)
    ensure_dir(folder / ".stockflow_work")


def safe_unique_path(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    n = 2
    while True:
        test = dest_dir / f"{stem}-{n}{suffix}"
        if not test.exists():
            return test
        n += 1


def slugify(text: str, max_len: int = 80) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        text = "image"
    return text[:max_len].strip("-") or "image"


def rename_from_title(title: str, original: Path, status: str) -> str:
    base = slugify(title if title else original.stem)
    ext = ".jpg" if status in {STATUS_READY, STATUS_REVIEW, STATUS_NEEDS_RELEASE, STATUS_LOWQUALITY} else original.suffix.lower()
    return f"{base}{ext}"


def move_into_folder(src: Path, folder: Path, new_name: str | None = None) -> Path:
    ensure_dir(folder)
    target_name = new_name or src.name
    target = safe_unique_path(folder, target_name)
    return Path(shutil.move(str(src), str(target)))


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_exiftool() -> bool:
    try:
        subprocess.run([EXIFTOOL, "-ver"], capture_output=True, text=True, check=True)
        return True
    except Exception:
        return False

# ──────────────────────────── IMAGE PROCESSING ────────────────────────────

def resize_for_api(path: Path) -> bytes:
    img = Image.open(path).convert("RGB")
    img.thumbnail((MAX_PIXELS, MAX_PIXELS), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def normalize_image(path: Path, work_dir: Path) -> Path:
    size_mb = path.stat().st_size / (1024 * 1024)
    needs_convert = path.suffix.lower() == ".png"
    needs_shrink = size_mb > MAX_FILE_SIZE_MB
    if not needs_convert and not needs_shrink:
        return path

    ensure_dir(work_dir)
    out_path = work_dir / f"{path.stem}_ready.jpg"

    img = Image.open(path).convert("RGB")
    quality = 92
    img.save(out_path, format="JPEG", quality=quality)

    while out_path.stat().st_size / (1024 * 1024) > MAX_FILE_SIZE_MB and quality > 60:
        quality -= 10
        img.save(out_path, format="JPEG", quality=quality)

    scale = 1.0
    while out_path.stat().st_size / (1024 * 1024) > MAX_FILE_SIZE_MB and scale > 0.5:
        scale -= 0.1
        w, h = img.size
        resized = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        resized = resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
        resized.save(out_path, format="JPEG", quality=75)

    return out_path


def extract_json_text(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def validate_response(data: dict):
    required = ["title", "description", "keywords", "category", "commercial_score", "rejection_risk"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Model response missing keys: {missing}")
    if not isinstance(data["keywords"], list) or not data["keywords"]:
        raise ValueError("Model returned no usable keywords")


def call_gemini(client, image_bytes: bytes, max_retries: int = 3) -> dict:
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


def embed_metadata(path: Path, title: str, description: str, keywords: list[str]):
    kw_args = []
    for kw in keywords:
        kw_args += [f"-Keywords+={kw}", f"-XMP-dc:Subject+={kw}"]
    cmd = [
        EXIFTOOL,
        "-overwrite_original",
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


def write_csv_row(csv_path: Path, filename: str, title: str, keywords: list[str], category: str, category2: str):
    categories = ", ".join(c for c in (category, category2) if c)
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["Filename", "Description", "Keywords", "Categories"])
        w.writerow([filename, title, ", ".join(keywords), categories])


def log(review_log: Path, line: str):
    with open(review_log, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def short_review_reason(data: dict) -> str:
    reason = data.get("rejection_reason", "").strip()
    return reason


def choose_status(score: int, risk: str, people_visible: bool, property_visible: bool) -> str:
    if people_visible or property_visible:
        return STATUS_NEEDS_RELEASE
    if score < MIN_SCORE:
        return STATUS_LOWQUALITY
    if risk in {"Medium", "High"}:
        return STATUS_REVIEW
    return STATUS_READY

# ──────────────────────────── REPORTING ───────────────────────────────────

def write_report(report_dir: Path, records: list[dict], summary: dict):
    ensure_dir(report_dir)
    json_path = report_dir / "report.json"
    csv_path = report_dir / "report.csv"

    json_path.write_text(json.dumps({"summary": summary, "items": records}, indent=2, ensure_ascii=False), encoding="utf-8")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "original_name", "final_name", "status", "score", "risk", "category",
            "category2", "title", "destination", "reason", "people_visible",
            "property_or_trademark_visible"
        ])
        for r in records:
            writer.writerow([
                r.get("original_name", ""),
                r.get("final_name", ""),
                r.get("status", ""),
                r.get("score", ""),
                r.get("risk", ""),
                r.get("category", ""),
                r.get("category2", ""),
                r.get("title", ""),
                r.get("destination", ""),
                r.get("reason", ""),
                r.get("people_visible", False),
                r.get("property_or_trademark_visible", False),
            ])

# ──────────────────────────── MAIN ────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print('Usage:  python stockflow.py "D:\\Photos\\batch_01"')
        sys.exit(1)

    folder = Path(sys.argv[1]).expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        sys.exit(1)

    ensure_output_dirs(folder)

    if not check_exiftool():
        print(f"\nCan't run exiftool ({EXIFTOOL}).")
        print("Put exiftool.exe in the same folder as this script, or make sure 'exiftool' is on your PATH.")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\nGEMINI_API_KEY not found.")
        print('Run this in PowerShell:  setx GEMINI_API_KEY "your-key-here"')
        print("Then close and reopen PowerShell, and try again.\n")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    registry = load_registry(folder)

    csv_path = folder / FOLDER_REPORTS / "shutterstock_upload.csv"
    review_log = folder / FOLDER_REPORTS / "needs_review.txt"
    source_archive = folder / FOLDER_SOURCE_ORIGINALS
    ready_dir = folder / FOLDER_READY
    lowres_dir = folder / FOLDER_LOWRES
    lowquality_dir = folder / FOLDER_LOWQUALITY
    dup_dir = folder / FOLDER_DUPLICATES
    needs_release_dir = folder / FOLDER_NEEDS_RELEASE
    review_dir = folder / FOLDER_REVIEW
    report_dir = folder / FOLDER_REPORTS
    work_dir = folder / ".stockflow_work"

    all_images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_TYPES and is_pending(p.name, registry)
    )

    if not all_images:
        print("Nothing left to process in this folder (or everything already handled).")
        return

    batch = all_images[:BATCH_LIMIT]
    print(f"\nFolder: {folder}")
    print(f"Pending: {len(all_images)} images. Processing {len(batch)} this run.")
    print(f"Skipping anything scored below {MIN_SCORE}/100 or under {MIN_MEGAPIXELS}MP.\n")
    print(f"Output folders will be created under: {folder}\n")

    seen_hashes: dict[str, str] = {}
    records: list[dict] = []

    counts = {
        STATUS_READY: 0,
        STATUS_LOWRES: 0,
        STATUS_LOWQUALITY: 0,
        STATUS_DUPLICATE: 0,
        STATUS_NEEDS_RELEASE: 0,
        STATUS_REVIEW: 0,
        STATUS_ERROR: 0,
        STATUS_ERROR_PERMANENT: 0,
    }

    for i, path in enumerate(batch, 1):
        print(f"[{i}/{len(batch)}] {path.name}", end=" ... ", flush=True)
        names = {path.name}
        work_path = path
        prepared = False
        temp_created = False
        file_hash = None

        try:
            file_hash = compute_sha256(path)
            if file_hash in seen_hashes:
                print(f"DUPLICATE (same as {seen_hashes[file_hash]})")
                dup_name = rename_from_title("", path, STATUS_DUPLICATE)
                dup_final = move_into_folder(path, dup_dir, dup_name)
                mark_registry(folder, registry, names, STATUS_DUPLICATE, attempts=0, destination=FOLDER_DUPLICATES, final_name=dup_final.name, hash=file_hash, duplicate_of=seen_hashes[file_hash])
                counts[STATUS_DUPLICATE] += 1
                records.append({
                    "original_name": path.name,
                    "final_name": dup_final.name,
                    "status": STATUS_DUPLICATE,
                    "destination": FOLDER_DUPLICATES,
                    "hash": file_hash,
                    "duplicate_of": seen_hashes[file_hash],
                })
                continue

            seen_hashes[file_hash] = path.name

            work_path = normalize_image(path, work_dir)
            prepared = work_path != path
            temp_created = prepared
            if prepared:
                print(f"(prepared as {work_path.name}) ", end="", flush=True)
                log(review_log, f"NOTE  {path.name}  auto-prepared for upload -> {work_path.name}")

            with Image.open(work_path) as probe:
                mp = (probe.size[0] * probe.size[1]) / 1_000_000
            if mp < MIN_MEGAPIXELS:
                print(f"SKIPPED low-res ({mp:.1f}MP, need {MIN_MEGAPIXELS}MP+)")
                final_name = rename_from_title("", path, STATUS_LOWRES)
                dest = move_into_folder(path, lowres_dir, final_name)
                if prepared and work_path.exists():
                    work_path.unlink(missing_ok=True)
                mark_registry(folder, registry, names, STATUS_LOWRES, attempts=0, destination=FOLDER_LOWRES, final_name=dest.name, megapixels=round(mp, 2), hash=file_hash)
                counts[STATUS_LOWRES] += 1
                records.append({
                    "original_name": path.name,
                    "final_name": dest.name,
                    "status": STATUS_LOWRES,
                    "destination": FOLDER_LOWRES,
                    "megapixels": round(mp, 2),
                    "hash": file_hash,
                })
                continue

            img_bytes = resize_for_api(work_path)
            data = call_gemini(client, img_bytes)

            score = int(data.get("commercial_score", 0))
            risk = str(data.get("rejection_risk", "Unknown")).strip()
            reason = short_review_reason(data)
            people_visible = bool(data.get("people_visible", False))
            property_visible = bool(data.get("property_or_trademark_visible", False))
            category = str(data.get("category", "")).strip()
            category2 = str(data.get("category2", "")).strip()

            if category not in SHUTTERSTOCK_CATEGORIES:
                log(review_log, f"BAD CATEGORY  {path.name}  got='{category}'")
                category = "Miscellaneous"
            if category2 and (category2 not in SHUTTERSTOCK_CATEGORIES or category2 == category):
                category2 = ""

            status = choose_status(score, risk, people_visible, property_visible)

            # store a human-readable explanation in the review log for anything not READY
            if status in {STATUS_NEEDS_RELEASE, STATUS_REVIEW, STATUS_LOWQUALITY} and reason:
                log(review_log, f"{status}  {path.name}  {reason}")
            if status == STATUS_NEEDS_RELEASE:
                if people_visible:
                    log(review_log, f"FLAG  {path.name}  PEOPLE — model release needed")
                if property_visible:
                    log(review_log, f"FLAG  {path.name}  PROPERTY/TRADEMARK — release may be needed")

            title = str(data["title"]).strip()
            description = str(data["description"]).strip()
            keywords = [k.strip() for k in data["keywords"] if str(k).strip()]

            final_name = rename_from_title(title, path, status)
            destination_dir = {
                STATUS_READY: ready_dir,
                STATUS_LOWQUALITY: lowquality_dir,
                STATUS_NEEDS_RELEASE: needs_release_dir,
                STATUS_REVIEW: review_dir,
            }.get(status, review_dir)

            # write metadata for anything potentially usable later
            if status in {STATUS_READY, STATUS_NEEDS_RELEASE, STATUS_REVIEW}:
                embed_metadata(work_path, title, description, keywords)

            # move the processed file into its final bucket
            if prepared:
                moved_final = move_into_folder(work_path, destination_dir, final_name)
                # archive the original source so the working folder becomes clean
                if path.exists():
                    move_into_folder(path, source_archive, path.name)
            else:
                moved_final = move_into_folder(path, destination_dir, final_name)

            if status == STATUS_READY:
                write_csv_row(csv_path, moved_final.name, title, keywords, category, category2)

            mark_registry(
                folder,
                registry,
                names,
                status,
                attempts=0,
                destination=destination_dir.name,
                final_name=moved_final.name,
                score=score,
                risk=risk,
                category=category,
                category2=category2,
                hash=file_hash,
                people_visible=people_visible,
                property_or_trademark_visible=property_visible,
            )

            counts[status] += 1

            records.append({
                "original_name": path.name,
                "final_name": moved_final.name,
                "status": status,
                "score": score,
                "risk": risk,
                "category": category,
                "category2": category2,
                "title": title,
                "destination": destination_dir.name,
                "reason": reason,
                "people_visible": people_visible,
                "property_or_trademark_visible": property_visible,
                "hash": file_hash,
            })

            if status == STATUS_READY:
                print(f"READY  score={score}/100  risk={risk}  keywords={len(keywords)}")
            elif status == STATUS_LOWQUALITY:
                print(f"LOW QUALITY  score={score}/100  risk={risk}")
            elif status == STATUS_REVIEW:
                print(f"REVIEW  score={score}/100  risk={risk}")
            elif status == STATUS_NEEDS_RELEASE:
                print(f"NEEDS RELEASE  score={score}/100  risk={risk}")
            else:
                print(f"{status}  score={score}/100  risk={risk}")

        except Exception as e:
            attempts = registry.get(path.name, {}).get("attempts", 0) + 1
            status = STATUS_ERROR if attempts < MAX_ATTEMPTS else STATUS_ERROR_PERMANENT
            print(f"ERROR (attempt {attempts}/{MAX_ATTEMPTS}) — {e}")
            log(review_log, f"ERROR  {path.name}  attempt={attempts}  {e}")
            mark_registry(folder, registry, names, status, attempts=attempts, last_error=str(e)[:300], hash=file_hash)
            counts[status] += 1
            records.append({
                "original_name": path.name,
                "final_name": "",
                "status": status,
                "error": str(e),
                "hash": file_hash,
            })

        time.sleep(PAUSE_SECONDS)

    summary = {
        "processed_this_run": len(batch),
        "ready_to_upload": counts[STATUS_READY],
        "low_res": counts[STATUS_LOWRES],
        "low_quality": counts[STATUS_LOWQUALITY],
        "duplicates": counts[STATUS_DUPLICATE],
        "needs_release": counts[STATUS_NEEDS_RELEASE],
        "review": counts[STATUS_REVIEW],
        "errors": counts[STATUS_ERROR] + counts[STATUS_ERROR_PERMANENT],
        "remaining": len(all_images) - len(batch),
        "folder": str(folder),
    }

    write_report(report_dir, records, summary)

    print(f"\n── Done ─────────────────────────────────────────")
    print(f"   Processed this run    : {len(batch)}")
    print(f"   Ready to upload       : {counts[STATUS_READY]}")
    print(f"   Low resolution        : {counts[STATUS_LOWRES]}")
    print(f"   Low quality           : {counts[STATUS_LOWQUALITY]}")
    print(f"   Duplicates            : {counts[STATUS_DUPLICATE]}")
    print(f"   Needs release         : {counts[STATUS_NEEDS_RELEASE]}")
    print(f"   Review                : {counts[STATUS_REVIEW]}")
    print(f"   Errors                : {counts[STATUS_ERROR] + counts[STATUS_ERROR_PERMANENT]}")
    print(f"   Still pending         : {summary['remaining']} (run again to continue)")
    print(f"   CSV ready at          : {csv_path}")
    print(f"   Report JSON           : {report_dir / 'report.json'}")
    print(f"   Report CSV            : {report_dir / 'report.csv'}")
    if review_log.exists():
        print(f"   Review log            : {review_log}")
    print()


if __name__ == "__main__":
    main()
