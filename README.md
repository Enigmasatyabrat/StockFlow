"""
========================================================
  STOCK METADATA PIPELINE  —  v3 (fixed + sell-optimized + auto-prep)
  Free. Local. No subscriptions.
========================================================

WHAT THIS DOES:
  Looks at each photo with AI, writes a SALES-OPTIMIZED title +
  description + keywords INSIDE the file, skips weak/blurry/too-small
  shots, flags photos that need a model/property release, AUTOMATICALLY
  fixes file-format/size problems so the output is upload-ready, and
  makes a CSV so Shutterstock auto-fills on upload. Adobe Stock also
  reads the embedded metadata automatically.

  After this runs, the only manual step left is dragging the folder
  into Shutterstock / Adobe Stock / wherever else you're submitting.

WHAT'S NEW IN v3 (on top of the v2 fixes):
  - Auto file-prep ("the image resizer"): any PNG gets converted to a
    JPEG copy (Shutterstock requires JPEG/TIFF, not PNG), and any file
    over Shutterstock's 50MB limit gets automatically re-compressed/
    resized down until it fits — instead of just warning you about it
    and leaving you to fix it by hand. Your original files are never
    touched; a "_ready.jpg" copy is created next to them when needed.
  - A double-click launcher (run_pipeline.bat) so you never have to
    open PowerShell and type a command — just drag your photo folder
    onto the .bat file (see the bottom of this docstring).

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

  EASIEST WAY:
  Double-click "run_pipeline.bat" (next to this script) and either
  drag your photo folder onto its icon, or just double-click it and
  paste the folder path when it asks. No PowerShell, no typing python
  commands.

  MANUAL WAY:
  python stock_pipeline_v3.py "D:\\Photos\\batch_01"

  Replace the path with your actual folder of photos.
  It will create two files inside that folder:
    - shutterstock_upload.csv   (import this on Shutterstock)
    - needs_review.txt          (skips, errors, AND release/rejection/
                                  auto-prep notes worth reading before
                                  upload)

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