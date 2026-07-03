# StockFlow

**Automate metadata generation, image preparation, and stock submission workflows.**

StockFlow reduces the repetitive work of preparing stock photography for marketplaces like Shutterstock and Adobe Stock. It analyzes photos, generates commercial metadata, embeds it into the image files, prepares upload-ready copies, and creates Shutterstock-compatible CSV output.

---

## What StockFlow Does

* Generate commercial stock metadata with Gemini Vision
* Create SEO-oriented titles, descriptions, and keywords
* Embed EXIF, IPTC, and XMP metadata
* Batch process entire photo collections
* Export Shutterstock-compatible CSV files
* Support Adobe Stock-compatible embedded metadata
* Convert PNG files to JPEG automatically
* Prepare oversized images for upload
* Validate resolution before processing
* Organize files into workflow folders automatically
* Detect exact duplicates using SHA-256
* Flag near-duplicates using perceptual hashing (pHash)
* Improve model and property release detection
* Reduce hallucinated metadata with stronger prompt rules
* Produce JSON and CSV processing reports
* Show runtime diagnostics and processing statistics
* Retry Gemini API calls intelligently during temporary failures

---

## Workflow

```text
Photo Folder
      │
      ▼
StockFlow
      │
      ├── AI metadata generation
      ├── EXIF embedding
      ├── Upload preparation
      ├── CSV generation
      └── Review log
      │
      ▼
Upload
      ├── Shutterstock
      ├── Adobe Stock
      ├── Alamy
      └── Other agencies
```

---

## Requirements

* Python 3.11+
* Google Gemini API key
* ExifTool

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Install Python packages

```bash
pip install -r requirements.txt
```

### 2. Download ExifTool

Download ExifTool and place `exiftool.exe` beside `stockflow.py`.

### 3. Configure Gemini

Set your API key:

```powershell
setx GEMINI_API_KEY "YOUR_API_KEY"
```

Restart your terminal after setting it.

---

## Usage

### Windows

Double-click:

```text
run_stockflow.bat
```

Or run manually:

```bash
python stockflow.py "D:\Photos\Batch1"
```

---

## Output

StockFlow generates:

* `shutterstock_upload.csv`
* `needs_review.txt`
* `report.json`
* `report.csv`

It also embeds metadata directly into supported image files.

---

## Roadmap

* Duplicate image similarity improvements
* Blur detection
* Noise analysis
* Portfolio database
* Multi-agency upload support
* Contributor analytics dashboard

---

## License

Apache License 2.0.

---

## Author

Satyabrat Mishra

---

## Disclaimer

StockFlow assists with metadata generation and workflow automation. Contributors remain responsible for verifying metadata accuracy and complying with the submission requirements of each stock marketplace, including model releases, property releases, and intellectual property restrictions.
