"""Marketplace-specific submission rules.

Every constant here was checked against official contributor documentation in
August 2026. Sources are cited per-rule because these specs change and a
confidently-wrong CSV header silently corrupts an entire upload batch.
"""

from __future__ import annotations

# --------------------------------------------------------------- Shutterstock

#: Verified against submit.shutterstock.com's "How do I include existing
#: metadata with my content submission?" help article. Columns A-D are
#: required; E-G are optional. The docs warn: "If your CSV file is not
#: formatted exactly as shown in the sample above, the file will be rejected."
SHUTTERSTOCK_HEADER = ["Filename", "Description", "Keywords", "Categories"]

#: Optional Shutterstock columns, documented but not currently emitted.
SHUTTERSTOCK_OPTIONAL_COLUMNS = ["Illustration", "Mature Content", "Editorial"]

#: Shutterstock allows one or two categories, comma-separated.
SHUTTERSTOCK_MAX_CATEGORIES = 2

#: Widely-documented keyword range. The official article states only that
#: keywords are "separated by commas" without giving limits, so the minimum is
#: advisory: StockFlow warns below it rather than refusing to submit.
SHUTTERSTOCK_MIN_KEYWORDS = 7
SHUTTERSTOCK_MAX_KEYWORDS = 50


# --------------------------------------------------------------- Adobe Stock

#: Verified byte-for-byte against Adobe's own downloadable template at
#: contributor.stock.adobe.com/static/csv/Sample_Adobe_Stock_CSV_upload.csv
ADOBE_HEADER = ["Filename", "Title", "Keywords", "Category", "Releases"]

#: Adobe requires the CSV filename to match the uploaded asset exactly and to
#: be 30 characters or fewer.
ADOBE_MAX_FILENAME_CHARS = 30

#: Adobe's own sources disagree: the help pages say titles must be 70
#: characters or fewer, while the sample CSV template says "Up to 200
#: characters". StockFlow does not truncate -- silently cutting a title
#: mid-sentence is worse than a visible rejection -- but warns past the
#: stricter figure.
ADOBE_TITLE_SOFT_LIMIT = 70
ADOBE_TITLE_HARD_LIMIT = 200

ADOBE_MAX_KEYWORDS = 50

#: Adobe's Category column takes a NUMBER, not a name -- the sample row uses
#: `3`. Full list from the "Choose the right category in the Adobe Stock
#: Contributor portal" help page.
ADOBE_CATEGORIES: dict[int, str] = {
    1: "Animals",
    2: "Buildings and Architecture",
    3: "Business",
    4: "Drinks",
    5: "The Environment",
    6: "States of Mind",
    7: "Food",
    8: "Graphic Resources",
    9: "Hobbies and Leisure",
    10: "Industry",
    11: "Landscape",
    12: "Lifestyle",
    13: "People",
    14: "Plants and Flowers",
    15: "Culture and Religion",
    16: "Science",
    17: "Social Issues",
    18: "Sports",
    19: "Technology",
    20: "Transport",
    21: "Travel",
}

#: Best-effort mapping from the Shutterstock category the model picks to
#: Adobe's numbering. The two taxonomies do not line up one-to-one, so several
#: of these are judgement calls rather than equivalences -- Shutterstock's
#: "The Arts", "Education" and "Vintage" have no Adobe counterpart at all.
#: Adobe treats Category as optional and auto-suggests one via Sensei, so an
#: unmapped value is left blank rather than forced into a wrong bucket.
_SHUTTERSTOCK_TO_ADOBE: dict[str, int] = {
    "Abstract": 8,                  # Graphic Resources
    "Animals/Wildlife": 1,          # Animals
    "Backgrounds/Textures": 8,      # Graphic Resources
    "Beauty/Fashion": 12,           # Lifestyle
    "Buildings/Landmarks": 2,       # Buildings and Architecture
    "Business/Finance": 3,          # Business
    "Celebrities": 13,              # People
    "Food and Drink": 7,            # Food
    "Healthcare/Medical": 16,       # Science
    "Holidays": 15,                 # Culture and Religion
    "Industrial": 10,               # Industry
    "Interiors": 2,                 # Buildings and Architecture
    "Nature": 5,                    # The Environment
    "Objects": 8,                   # Graphic Resources
    "Parks/Outdoor": 11,            # Landscape
    "People": 13,                   # People
    "Religion": 15,                 # Culture and Religion
    "Science": 16,                  # Science
    "Signs/Symbols": 8,             # Graphic Resources
    "Sports/Recreation": 18,        # Sports
    "Technology": 19,               # Technology
    "Transportation": 20,           # Transport
    # Deliberately unmapped -- no honest Adobe equivalent:
    #   "The Arts", "Education", "Miscellaneous", "Vintage"
}


def adobe_category_number(shutterstock_category: str) -> str:
    """Adobe category number for a Shutterstock category name.

    Returns an empty string when there is no honest mapping, which leaves the
    column blank so Adobe's own auto-suggestion applies.
    """
    number = _SHUTTERSTOCK_TO_ADOBE.get(shutterstock_category)
    return str(number) if number else ""


def adobe_title(title: str) -> str:
    """Adapt a title for Adobe's Title column.

    Adobe documents that the title should carry no commas. The value is quoted
    in the CSV either way, so this is about their parser's expectations rather
    than about escaping.
    """
    return " ".join(title.replace(",", " ").split())


def filename_warnings(filename: str) -> list[str]:
    """Marketplace complaints about a proposed output filename."""
    warnings = []
    if len(filename) > ADOBE_MAX_FILENAME_CHARS:
        warnings.append(
            f"filename is {len(filename)} characters; Adobe Stock requires "
            f"{ADOBE_MAX_FILENAME_CHARS} or fewer"
        )
    return warnings


def keyword_warnings(count: int) -> list[str]:
    warnings = []
    if count < SHUTTERSTOCK_MIN_KEYWORDS:
        warnings.append(
            f"only {count} keyword(s); Shutterstock submissions are widely "
            f"reported to need at least {SHUTTERSTOCK_MIN_KEYWORDS}"
        )
    return warnings


def title_warnings(title: str) -> list[str]:
    warnings = []
    if len(title) > ADOBE_TITLE_HARD_LIMIT:
        warnings.append(
            f"title is {len(title)} characters; Adobe Stock's template allows "
            f"{ADOBE_TITLE_HARD_LIMIT}"
        )
    return warnings
