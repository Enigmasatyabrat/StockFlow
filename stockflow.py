
"""
StockFlow v4.1
=============
AI-powered workflow for preparing stock photography for Shutterstock,
Adobe Stock, and other stock marketplaces.

v4.1 focus (on top of v4):
- distinguishes daily-quota exhaustion (stop cleanly, no blacklisting)
  from short rate-limit bursts and transient 503 overload (retry both,
  using Google's own suggested retry delay when it's given to us)
- every classification now carries a guaranteed, human-readable reason
- keyword post-processing: dedupe, drop crude singular/plural repeats,
  hard cap at 50
- perceptual-hash near-duplicate flagging alongside the existing exact
  (SHA-256) duplicate detection — logged, not auto-moved, since visually
  similar shots are often still independently worth submitting
- tightened prompt: explicit scoring rubric, anti-hallucination guard,
  and tighter rules for when people/property should actually be flagged
- startup diagnostics banner + richer end-of-run report (duration,
  average score, average keyword count, category mix, retry counts)

Author: Satyabrat Mishra
License: Apache-2.0
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import imagehash
from google import genai
from google.genai import types
from PIL import Image, ImageFilter

# ──────────────────────────── SETTINGS ────────────────────────────────────
SCRIPT_DIR       = Path(__file__).resolve().parent
MODEL            = "gemini-2.5-flash-lite"
BATCH_LIMIT      = 50
PAUSE_SECONDS    = 4
MIN_SCORE        = 60
MAX_PIXELS       = 1024
MIN_MEGAPIXELS   = 4.0
MAX_FILE_SIZE_MB = 50
MAX_ATTEMPTS     = 3
IMAGE_TYPES      = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
DUPLICATE_HASH_THRESHOLD = 6   # perceptual-hash Hamming distance (0-64); lower = stricter
VERSION = "4.1.0"


class DailyQuotaExhausted(Exception):
    """Raised when the model's whole-day free quota is gone — not the photo's fault,
    so the run should stop cleanly instead of retrying or blacklisting anything."""
    pass

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
microstock marketplaces. You have personally reviewed tens of thousands of
submissions and know exactly what separates a top-selling asset from a
forgettable one. Buyers find images almost entirely through search, so the
title and keywords must be written in language buyers actually type — not
artistic or poetic language.

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

ANTI-HALLUCINATION RULE (applies to every field below): only describe what
you can actually see. If you are not confident about a species, location,
brand, material, or backstory, leave it out rather than guess. A vague-but-
true keyword beats a specific-but-wrong one — wrong facts get submissions
rejected and can get a contributor account flagged.

TITLE — this also becomes the Shutterstock "Description" field, which
functions like a searchable headline, not a caption:
- First identify, mentally, the single concept a buyer would most likely
  type into a search box to find this exact image. Lead the title with it.
- Strictly factual: describe what is literally visible.
- 8-18 words, under 180 characters. No camera/lens jargon ("shot on",
  "bokeh", "f/2.8"). No filler like "stock photo of" or "image showing".
- Good: "Woman drinking coffee at laptop in sunlit home office"
- Bad (too vague, no buyer would search this): "A nice moment indoors"
- Bad (camera jargon, buyers don't search this way): "Shallow DOF shot of
  a woman, 50mm f/1.8"

DESCRIPTION — used only in the embedded file metadata:
- 1-2 plain sentences. Concrete subject + setting + likely use, in your own
  words. Don't just restate the title.

KEYWORDS — 40 to 50 terms, ordered MOST to LEAST important. This is the
single biggest driver of whether this image ever gets found:
- Layer 1 (literal): every concrete subject, object, and setting actually
  visible in the frame.
- Layer 2 (concept): emotions/ideas a buyer searches by (teamwork, freedom,
  growth, mindfulness) — ONLY if visually justified, never invented.
- Layer 3 (use-case): commercial terms buyers search for (copy space,
  background, banner, advertising, website, blog, presentation).
- Layer 4 (descriptive): color, lighting, composition, season, time of day.
- Use singular nouns unless the plural is the natural search term.
- No keyword stuffing — every term must genuinely apply.

CATEGORY / CATEGORY2:
- "category" is REQUIRED — pick exactly ONE from this list:
  Abstract, Animals/Wildlife, The Arts, Backgrounds/Textures, Beauty/Fashion,
  Buildings/Landmarks, Business/Finance, Celebrities, Education, Food and Drink,
  Healthcare/Medical, Holidays, Industrial, Interiors, Miscellaneous, Nature,
  Objects, Parks/Outdoor, People, Religion, Science, Signs/Symbols,
  Sports/Recreation, Technology, Transportation, Vintage
- "category2" is OPTIONAL — fill it only if clearly applicable.

COMMERCIAL_SCORE: integer 0-100, built from this rubric so scores stay
consistent across images rather than feeling like a vibe check:
- Technical quality (sharpness, exposure, noise) — up to 30 points
- Composition (framing, negative space, clutter) — up to 25 points
- Market demand (is this a subject buyers actually search for?) — up to 25
- Distinctiveness (does it stand out from the thousands of similar stock
  shots of this same subject, or is it generic?) — up to 20

REJECTION_RISK: exactly one of — Low / Medium / High, based on the same
rubric above.

REJECTION_REASON: if risk is Medium or High, ONE short, specific, actionable
sentence naming the actual weak point (e.g. "Slight motion blur on the main
subject's hands" or "Extremely common subject with no distinctive angle").
Never leave this blank when risk is Medium or High — if you're not sure,
say "Generally below the commercial bar for this subject matter" rather
than returning an empty string. If risk is Low, return "".

PEOPLE_VISIBLE: true ONLY if an actual real, identifiable human face or
body is visible, even partially. Do NOT mark this true for: statues,
mannequins, paintings/illustrations of people, reflections that are too
distorted to identify, or silhouettes with no identifiable features. When
genuinely unsure whether a face is identifiable, default to true — false
negatives here cause real legal exposure for the contributor, so err
toward caution, but don't flag things that obviously aren't people.

PROPERTY_OR_TRADEMARK_VISIBLE: true ONLY if a clearly recognizable logo,
branded product packaging, distinctive copyrighted artwork, or a
specific, identifiable piece of private architecture/property is in focus
and central to the frame — not just incidentally visible in the
background at a scale where it isn't legible or recognizable.

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


def compute_phash(path: Path) -> Optional[str]:
    """Perceptual hash for near-duplicate detection (burst shots, slight
    crops/recompressions). Returns None rather than raising — this is a
    nice-to-have signal, never worth breaking the run over."""
    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception:
        return None


def find_near_duplicate(phash_hex: Optional[str], seen_phashes: Dict[str, str],
                         threshold: int = DUPLICATE_HASH_THRESHOLD) -> Optional[str]:
    """Returns the filename of a visually-similar image already seen this run,
    or None. Never moves or rejects anything by itself — just a signal."""
    if not phash_hex:
        return None
    try:
        this_hash = imagehash.hex_to_hash(phash_hex)
    except Exception:
        return None
    for other_name, other_hex in seen_phashes.items():
        try:
            if (this_hash - imagehash.hex_to_hash(other_hex)) <= threshold:
                return other_name
        except Exception:
            continue
    return None


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


def clean_keywords(keywords: List[str], max_keywords: int = 50) -> List[str]:
    """Dedupes (case-insensitive), drops crude singular/plural repeats, and
    hard-caps the list. Conservative on purpose: worst case it leaves a
    harmless near-duplicate in, it never silently invents or drops a
    keyword that doesn't look like a repeat."""
    seen: set = set()
    cleaned: List[str] = []
    for kw in keywords:
        kw = str(kw).strip()
        if len(kw) < 2:
            continue
        key = kw.lower()
        if key in seen:
            continue
        singular_guess = key[:-1] if key.endswith("s") and len(key) > 3 else None
        plural_guess = key + "s"
        if (singular_guess and singular_guess in seen) or plural_guess in seen:
            continue
        seen.add(key)
        cleaned.append(kw)
    return cleaned[:max_keywords]


def parse_retry_seconds(message: str) -> Optional[float]:
    """Pulls Google's own suggested wait time out of the error text when
    it's there, instead of guessing blind."""
    m = re.search(r"retry in ([\d.]+)\s*s", message, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"retrydelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)\s*s", message, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def call_gemini(client, image_bytes: bytes, stats: Dict[str, int],
                 max_retries: int = 5) -> dict:
    """
    Calls Gemini with error-aware backoff:
      - daily quota gone (RESOURCE_EXHAUSTED + "PerDay") -> raises
        DailyQuotaExhausted immediately, no point retrying or blaming the photo
      - short rate-limit burst (RESOURCE_EXHAUSTED, no "PerDay") -> wait out
        Google's suggested delay (or a growing fallback) and retry
      - 503 / UNAVAILABLE / overloaded -> transient server overload, usually
        clears up; retry with exponential backoff + jitter
      - anything else -> short flat backoff and retry
    `stats` is a shared dict the caller can inspect afterward for reporting
    (e.g. stats["retries"] += 1 each time we wait and try again).
    """
    last_err: Optional[Exception] = None
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
            msg = str(e)
            msg_compact = msg.lower().replace(" ", "")

            if "resource_exhausted" in msg.lower() or " 429" in msg or "'code': 429" in msg:
                if "perday" in msg_compact or "requestsperday" in msg_compact:
                    raise DailyQuotaExhausted(msg) from e
                wait = parse_retry_seconds(msg) or (15 * attempt)
                wait += random.uniform(0, 3)
                kind = "rate limit"
            elif "503" in msg or "unavailable" in msg.lower() or "overloaded" in msg.lower():
                wait = parse_retry_seconds(msg) or min(10 * (2 ** (attempt - 1)), 90)
                wait += random.uniform(0, 5)
                kind = "server overload"
            else:
                wait = 3 * attempt
                kind = "error"

            stats["retries"] = stats.get("retries", 0) + 1
            if attempt == max_retries:
                break
            print(f" retry {attempt}/{max_retries} in {wait:.0f}s ({kind}: {msg[:90]})",
                  end=" ... ", flush=True)
            time.sleep(wait)
    stats["failures"] = stats.get("failures", 0) + 1
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


def choose_status(score, risk, people_visible, property_visible):
    if score < MIN_SCORE:
        return STATUS_LOWQUALITY
    if people_visible or property_visible:
        return STATUS_NEEDS_RELEASE
    if risk in {"Medium", "High"}:
        return STATUS_REVIEW
    return STATUS_READY


def explain_status(status: str, score: int, risk: str, model_reason: str) -> str:
    """Every classification gets a real explanation, even when the model's
    own rejection_reason came back blank — so nothing lands in a folder
    with no clue why."""
    model_reason = (model_reason or "").strip()
    if status == STATUS_READY:
        return model_reason or f"Meets the quality bar (score {score}/100, risk {risk})."
    if status == STATUS_LOWQUALITY:
        return model_reason or f"Commercial score {score}/100 is below the {MIN_SCORE} threshold."
    if status == STATUS_NEEDS_RELEASE:
        return model_reason or "Recognizable person or property detected — needs a release before commercial use."
    if status == STATUS_REVIEW:
        return model_reason or f"Elevated rejection risk ({risk}) — model didn't give a specific reason, check manually."
    return model_reason

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
    start_time = time.time()
    exiftool_ok = check_exiftool()
    api_key = os.environ.get("GEMINI_API_KEY")

    print("=" * 60)
    print(f"StockFlow v{VERSION}")
    print(f"Model               : {MODEL}")
    print(f"Batch size          : {BATCH_LIMIT}")
    print(f"Pause between calls : {PAUSE_SECONDS}s")
    print(f"Quality threshold   : {MIN_SCORE}/100")
    print(f"Resolution minimum  : {MIN_MEGAPIXELS}MP")
    print(f"ExifTool            : {'OK (' + EXIFTOOL + ')' if exiftool_ok else 'NOT FOUND'}")
    print(f"GEMINI_API_KEY      : {'set' if api_key else 'MISSING'}")
    print("=" * 60)

    if len(sys.argv) < 2:
        print('Usage:  python stockflow.py "D:\\Photos\\batch_01"')
        sys.exit(1)

    folder = Path(sys.argv[1]).expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        sys.exit(1)

    ensure_output_dirs(folder)

    if not exiftool_ok:
        print(f"\nCan't run exiftool ({EXIFTOOL}).")
        print("Put exiftool.exe in the same folder as this script, or make sure 'exiftool' is on your PATH.")
        sys.exit(1)

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

    seen_hashes: Dict[str, str] = {}
    seen_phashes: Dict[str, str] = {}
    records: List[dict] = []
    api_stats: Dict[str, int] = {"retries": 0, "failures": 0}
    daily_quota_stopped = False

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

    processed_count = 0

    for i, path in enumerate(batch, 1):
        print(f"[{i}/{len(batch)}] {path.name}", end=" ... ", flush=True)
        names = {path.name}
        work_path = path
        prepared = False
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
                    "reason": f"Byte-identical to {seen_hashes[file_hash]}.",
                    "hash": file_hash,
                    "duplicate_of": seen_hashes[file_hash],
                })
                processed_count += 1
                continue

            seen_hashes[file_hash] = path.name

            # near-duplicate flag is purely informational — never moves or
            # rejects anything, since visually similar shots (different
            # poses in a burst, slightly different crop) are often still
            # independently worth submitting
            phash = compute_phash(path)
            near_dupe_of = find_near_duplicate(phash, seen_phashes)
            if phash:
                seen_phashes[path.name] = phash
            if near_dupe_of:
                log(review_log, f"NOTE  {path.name}  visually similar to {near_dupe_of} — "
                                 f"check whether you need both before uploading")

            work_path = normalize_image(path, work_dir)
            prepared = work_path != path
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
                    "reason": f"{mp:.1f}MP is below the {MIN_MEGAPIXELS}MP minimum.",
                    "megapixels": round(mp, 2),
                    "hash": file_hash,
                })
                processed_count += 1
                continue

            img_bytes = resize_for_api(work_path)
            data = call_gemini(client, img_bytes, api_stats)

            score = int(data.get("commercial_score", 0))
            risk = str(data.get("rejection_risk", "Unknown")).strip()
            model_reason = short_review_reason(data)
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
            reason = explain_status(status, score, risk, model_reason)

            # every non-READY classification always gets a logged, readable reason now
            if status != STATUS_READY:
                log(review_log, f"{status}  {path.name}  score={score}  risk={risk}  {reason}")
            if status == STATUS_NEEDS_RELEASE:
                if people_visible:
                    log(review_log, f"FLAG  {path.name}  PEOPLE — model release needed")
                if property_visible:
                    log(review_log, f"FLAG  {path.name}  PROPERTY/TRADEMARK — release may be needed")

            title = str(data["title"]).strip()
            description = str(data["description"]).strip()
            keywords = clean_keywords([k for k in data["keywords"] if str(k).strip()])

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
            processed_count += 1

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
                "keyword_count": len(keywords),
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

        except DailyQuotaExhausted as e:
            daily_quota_stopped = True
            print("\n\nDaily Gemini quota reached — stopping here, not blaming this photo.")
            log(review_log, f"DAILY QUOTA EXHAUSTED  stopped at {path.name}  {e}")
            print(f"'{path.name}' and everything after it in this run is still marked")
            print("pending — nothing is lost. Free tier resets at midnight Pacific Time")
            print("(roughly 12:30 PM IST). Just run StockFlow again after that, or switch")
            print("MODEL to a less-restricted tier if you have one available.\n")
            break

        except Exception as e:
            attempts = registry.get(path.name, {}).get("attempts", 0) + 1
            status = STATUS_ERROR if attempts < MAX_ATTEMPTS else STATUS_ERROR_PERMANENT
            print(f"ERROR (attempt {attempts}/{MAX_ATTEMPTS}) — {e}")
            log(review_log, f"ERROR  {path.name}  attempt={attempts}  {e}")
            mark_registry(folder, registry, names, status, attempts=attempts, last_error=str(e)[:300], hash=file_hash)
            counts[status] += 1
            processed_count += 1
            records.append({
                "original_name": path.name,
                "final_name": "",
                "status": status,
                "reason": f"Failed after {attempts} attempt(s): {e}",
                "error": str(e),
                "hash": file_hash,
            })

        time.sleep(PAUSE_SECONDS)

    ready_records = [r for r in records if r.get("status") == STATUS_READY]
    scored_records = [r for r in records if isinstance(r.get("score"), int)]
    category_counts = Counter(r["category"] for r in ready_records if r.get("category"))

    duration_seconds = round(time.time() - start_time, 1)
    avg_score = round(sum(r["score"] for r in scored_records) / len(scored_records), 1) if scored_records else None
    avg_keywords = round(sum(r["keyword_count"] for r in ready_records) / len(ready_records), 1) if ready_records else None
    low_risk_ready = sum(1 for r in ready_records if r.get("risk") == "Low")
    acceptance_estimate = round(100 * low_risk_ready / len(ready_records), 1) if ready_records else None

    summary = {
        "version": VERSION,
        "model": MODEL,
        "processed_this_run": processed_count,
        "ready_to_upload": counts[STATUS_READY],
        "low_res": counts[STATUS_LOWRES],
        "low_quality": counts[STATUS_LOWQUALITY],
        "duplicates": counts[STATUS_DUPLICATE],
        "needs_release": counts[STATUS_NEEDS_RELEASE],
        "review": counts[STATUS_REVIEW],
        "errors": counts[STATUS_ERROR] + counts[STATUS_ERROR_PERMANENT],
        "remaining": len(all_images) - processed_count,
        "folder": str(folder),
        "duration_seconds": duration_seconds,
        "average_commercial_score": avg_score,
        "average_keyword_count": avg_keywords,
        "category_distribution": dict(category_counts),
        "api_retries": api_stats.get("retries", 0),
        "api_hard_failures": api_stats.get("failures", 0),
        "rough_acceptance_estimate_percent": acceptance_estimate,
        "stopped_on_daily_quota": daily_quota_stopped,
    }

    write_report(report_dir, records, summary)

    print(f"\n── Done ─────────────────────────────────────────")
    print(f"   Processed this run    : {processed_count}")
    print(f"   Ready to upload       : {counts[STATUS_READY]}")
    print(f"   Low resolution        : {counts[STATUS_LOWRES]}")
    print(f"   Low quality           : {counts[STATUS_LOWQUALITY]}")
    print(f"   Duplicates            : {counts[STATUS_DUPLICATE]}")
    print(f"   Needs release         : {counts[STATUS_NEEDS_RELEASE]}")
    print(f"   Review                : {counts[STATUS_REVIEW]}")
    print(f"   Errors                : {counts[STATUS_ERROR] + counts[STATUS_ERROR_PERMANENT]}")
    print(f"   Still pending         : {summary['remaining']} (run again to continue)")
    if avg_score is not None:
        print(f"   Average score         : {avg_score}/100")
    if avg_keywords is not None:
        print(f"   Average keyword count : {avg_keywords}")
    if acceptance_estimate is not None:
        print(f"   Rough acceptance est. : {acceptance_estimate}% (Low-risk share of Ready — a rough guide, not a guarantee)")
    print(f"   Duration              : {duration_seconds}s")
    print(f"   API retries / failures: {api_stats.get('retries', 0)} / {api_stats.get('failures', 0)}")
    print(f"   CSV ready at          : {csv_path}")
    print(f"   Report JSON           : {report_dir / 'report.json'}")
    print(f"   Report CSV            : {report_dir / 'report.csv'}")
    if review_log.exists():
        print(f"   Review log            : {review_log}")
    print()


if __name__ == "__main__":
    main()
