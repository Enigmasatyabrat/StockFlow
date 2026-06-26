# StockFlow

**AI-powered workflow for preparing stock photography for Shutterstock, Adobe Stock, and other stock marketplaces.**

StockFlow automates one of the most repetitive parts of stock photography: generating metadata, embedding it into image files, preparing upload-ready images, and creating Shutterstock-compatible CSV files.

Instead of manually writing titles, descriptions, and keywords for every image, StockFlow uses Google's Gemini Vision models to analyze photos and generate commercially focused metadata.

---

## Features

* AI-generated stock photo titles
* AI-generated descriptions
* SEO-oriented keyword generation
* EXIF/IPTC/XMP metadata embedding
* Shutterstock CSV export
* Adobe Stock compatible metadata
* Automatic PNG → JPEG conversion
* Automatic oversized image preparation
* Resolution validation
* Retry handling for temporary API failures
* Detection of potential model/property release requirements
* Batch processing

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
* Google Gemini API Key
* ExifTool

Python dependencies:

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

Restart your terminal.

---

## Usage

### Windows

Double-click:

```
run_stockflow.bat
```

or

```bash
python stockflow.py "D:\Photos\Batch1"
```

---

## Output

The pipeline generates:

* `shutterstock_upload.csv`
* `needs_review.txt`

and embeds metadata directly into supported image files.

---

## Roadmap

* Duplicate image detection
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

## Disclaimer

StockFlow assists with metadata generation and workflow automation. Contributors remain responsible for verifying metadata accuracy and complying with the submission requirements of each stock marketplace, including model releases, property releases, and intellectual property restrictions.
