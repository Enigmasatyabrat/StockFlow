"""The vision prompt and its response schema.

The schema is a plain dict rather than a Pydantic model on purpose. The SDK's
Pydantic transformer emits ``"default": ...`` keys for defaulted fields, and
the Gemini API has been reported to reject schemas containing them
(googleapis/python-genai#699). A hand-written dict avoids the question and
keeps the exact field ordering we want.

Because the schema now enforces structure -- key names, types, the category
enum, the 0-100 score range, the risk enum -- the prompt no longer restates
any of it. Google's structured-output guidance is explicit that repeating the
schema in the prompt degrades output quality.
"""

from __future__ import annotations

from .rules import SHUTTERSTOCK_CATEGORIES

#: Response schema handed to the API. OpenAPI subset, per Gemini's docs.
RESPONSE_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "description": {"type": "STRING"},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "category": {"type": "STRING", "enum": list(SHUTTERSTOCK_CATEGORIES)},
        # No empty string in this enum: the API rejects the whole request with
        # "enum[0]: cannot be empty". category2 is optional instead -- it is
        # absent from `required`, so the model omits it when there is no
        # sensible second category, and rules.normalize_category drops a value
        # that merely duplicates `category`.
        "category2": {"type": "STRING", "enum": list(SHUTTERSTOCK_CATEGORIES)},
        "commercial_score": {"type": "INTEGER"},
        "rejection_risk": {"type": "STRING", "enum": ["Low", "Medium", "High"]},
        "rejection_reason": {"type": "STRING"},
        "people_visible": {"type": "BOOLEAN"},
        "property_or_trademark_visible": {"type": "BOOLEAN"},
        "watermark_or_overlay_visible": {"type": "BOOLEAN"},
    },
    "required": [
        "title", "description", "keywords", "category", "commercial_score",
        "rejection_risk", "rejection_reason", "people_visible",
        "property_or_trademark_visible", "watermark_or_overlay_visible",
    ],
    "property_ordering": [
        "title", "description", "keywords", "category", "category2",
        "commercial_score", "rejection_risk", "rejection_reason",
        "people_visible", "property_or_trademark_visible",
        "watermark_or_overlay_visible",
    ],
}


SYSTEM_PROMPT = """You are a senior commercial stock photo editor and SEO \
copywriter for contributors selling on Shutterstock, Adobe Stock and similar \
microstock marketplaces. You have reviewed tens of thousands of submissions \
and know what separates a top seller from a forgettable one. Buyers find \
images almost entirely through search, so write in the language buyers \
actually type, not artistic or poetic language."""


ANTI_HALLUCINATION = """ONLY describe what you can actually see. If you are not \
confident about a species, location, brand, material or backstory, leave it \
out rather than guess. A vague-but-true keyword beats a specific-but-wrong \
one: wrong facts get submissions rejected and can get a contributor's account \
flagged."""


TASK_PROMPT = f"""Analyse the attached image as a commercial stock asset.

{ANTI_HALLUCINATION}

TITLE - this becomes the marketplace's searchable headline, not a caption.
- First decide the single concept a buyer would most likely type to find this
  exact image, and lead with it.
- Strictly factual: describe what is literally visible.
- 8-18 words, under 180 characters.
- No camera or lens jargon ("shot on", "bokeh", "f/2.8"). No filler such as
  "stock photo of" or "image showing".
- Good: "Woman drinking coffee at laptop in sunlit home office"
- Bad, too vague for anyone to search: "A nice moment indoors"

DESCRIPTION - embedded in the file metadata only.
- One or two plain sentences: concrete subject, setting, and likely use.
- Do not simply restate the title.

KEYWORDS - 40 to 50 terms, ordered MOST to LEAST important. This is the single
biggest driver of whether the image is ever found. Build them in layers:
- Literal: every concrete subject, object and setting visible in the frame.
- Conceptual: emotions and ideas buyers search by (teamwork, freedom, growth,
  mindfulness) - only where visually justified, never invented.
- Commercial: terms buyers filter on (copy space, background, banner,
  advertising, website, blog, presentation).
- Descriptive: colour, lighting, composition, season, time of day.
Use singular nouns unless the plural is the natural search term. Never repeat
a term, and never include a comma inside a single keyword. No keyword
stuffing - every term must genuinely apply.

CATEGORY / CATEGORY2 - category is required; category2 only if clearly
applicable, otherwise return an empty string.

COMMERCIAL_SCORE - integer 0-100, built from this rubric so scores stay
consistent across images rather than being a vibe check:
- Composition (framing, negative space, clutter) - up to 30
- Market demand (a subject buyers actually search for) - up to 30
- Distinctiveness (does it stand out from thousands of similar stock shots of
  the same subject, or is it generic?) - up to 25
- Technical quality - up to 15. If measured technical data is supplied below,
  base this on those figures rather than on your own impression.

REJECTION_RISK - exactly Low, Medium or High.

REJECTION_REASON - when risk is Medium or High, one short, specific,
actionable sentence naming the actual weak point (for example "Slight motion
blur on the subject's hands" or "Extremely common subject with no distinctive
angle"). Never leave it empty for Medium or High: if unsure, say "Generally
below the commercial bar for this subject matter". When risk is Low, return
an empty string.

PEOPLE_VISIBLE - true ONLY if a real, identifiable human face or body is
visible, even partially. NOT true for statues, mannequins, paintings or
illustrations of people, reflections too distorted to identify, or
silhouettes with no identifiable features. When genuinely unsure whether a
face is identifiable, answer true: a false negative here creates real legal
exposure for the contributor.

PROPERTY_OR_TRADEMARK_VISIBLE - true ONLY if a clearly recognisable logo,
branded packaging, distinctive copyrighted artwork, or a specific identifiable
piece of private architecture is in focus and central to the frame - not
merely incidental in the background at a size where it is not legible. This
field concerns intellectual property and identifiable buildings ONLY. Never
true for plants, flowers, insects, animals, generic landscapes or any other
natural subject: nature is not "property" in this sense, wherever it was
photographed.

WATERMARK_OR_OVERLAY_VISIBLE - true if there is ANY visible watermark,
photographer's logo stamp, social handle, URL, copyright notice or burned-in
text overlay anywhere in the frame, including small, faint or corner-placed
ones. Every marketplace auto-rejects on sight for this."""


def build_prompt(quality_note: str = "") -> str:
    """Assemble the per-image prompt, optionally with measured technical data."""
    if quality_note:
        return f"{TASK_PROMPT}\n\n{quality_note}"
    return TASK_PROMPT
