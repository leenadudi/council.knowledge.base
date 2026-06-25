"""
Vision LLM parser for complex PDFs (slide decks, dark backgrounds, colored tables).
Renders each page as an image, sends to Claude vision, extracts structured content.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

import anthropic
import pdf2image
from PIL import Image
import io

from src.config import Settings, get_settings
from src.models import ParsedDocument, ParsedElement

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """You are extracting content from a page of a government report.

Extract ALL visible text from this page, regardless of background color or text color.
Pay special attention to:
- White text on dark backgrounds
- Text in colored table cells
- Small print in headers/footers
- People's names and their titles or roles (from org charts, signature blocks, or narrative)
- Budget/financial table data with exact dollar amounts and account numbers

Return a JSON object with this structure:
{
  "page_type": "slide|table|narrative|org_chart|cover|mixed",
  "title": "page or slide title if present",
  "elements": [
    {
      "type": "Title|Header|NarrativeText|Table|ListItem|OrgChart",
      "text": "extracted text content",
      "notes": "optional: flag if uncertain about extraction"
    }
  ],
  "people": [
    {"name": "Full Name", "title": "Job Title or Role"}
  ],
  "extraction_confidence": "high|medium|low"
}

If a table is present, format its content as a plain-text grid preserving row/column structure.
If you cannot confidently extract something, include it with a note rather than omitting it.
"""


def parse(file_path: str | Path, settings: Settings | None = None) -> ParsedDocument:
    """
    Parse a complex PDF using a vision-capable LLM (Claude).
    Makes 1 LLM call per page.
    """
    cfg = settings or get_settings()
    path = Path(file_path)
    logger.info("Parsing %s with Vision LLM (%s)", path.name, cfg.vision_model)

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    images = _render_pages(path)

    all_elements: list[ParsedElement] = []

    for page_idx, image in enumerate(images):
        page_num = page_idx + 1
        try:
            page_elements = _extract_page(client, cfg, image, page_num)
            all_elements.extend(page_elements)
        except Exception as e:
            logger.warning("Vision extraction failed on page %d of %s: %s", page_num, path.name, e)
            # Add a placeholder so we don't silently lose the page
            all_elements.append(ParsedElement(
                element_type="NarrativeText",
                text=f"[Page {page_num} — extraction failed: {e}]",
                page_number=page_num,
            ))

    logger.info(
        "Vision LLM extracted %d elements from %d pages of %s",
        len(all_elements), len(images), path.name,
    )

    return ParsedDocument(
        source_file=path.name,
        parser_used="vision_llm",
        elements=all_elements,
        total_pages=len(images),
    )


def _render_pages(path: Path) -> list[Image.Image]:
    """Convert PDF pages to PIL images."""
    return pdf2image.convert_from_path(str(path), dpi=150)


def _encode_image(image: Image.Image) -> str:
    """Encode a PIL image to base64."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _extract_page(
    client: anthropic.Anthropic,
    cfg: Settings,
    image: Image.Image,
    page_num: int,
) -> list[ParsedElement]:
    """Send one page to the vision LLM and parse its response."""
    b64 = _encode_image(image)

    message = client.messages.create(
        model=cfg.vision_model,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    raw = message.content[0].text
    data = _parse_json_response(raw)

    elements: list[ParsedElement] = []
    for elem in data.get("elements", []):
        text = elem.get("text", "").strip()
        if not text:
            continue
        elements.append(ParsedElement(
            element_type=elem.get("type", "NarrativeText"),
            text=text,
            page_number=page_num,
            metadata={
                "page_type": data.get("page_type"),
                "extraction_confidence": data.get("extraction_confidence"),
                "notes": elem.get("notes"),
            },
        ))

    # Inline people as org_data elements so the classifier can pick them up
    for person in data.get("people", []):
        name = person.get("name", "").strip()
        title = person.get("title", "").strip()
        if name:
            elements.append(ParsedElement(
                element_type="OrgData",
                text=f"{name} — {title}" if title else name,
                page_number=page_num,
                metadata={"person_name": name, "person_title": title},
            ))

    return elements


def _parse_json_response(raw: str) -> dict:
    """Extract JSON from the LLM response (may be wrapped in markdown code fences)."""
    # Strip markdown code fences if present
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    json_str = match.group(1) if match else raw

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("Vision LLM returned non-JSON response, treating as narrative")
        return {"elements": [{"type": "NarrativeText", "text": raw}], "people": []}
