"""
Unstructured.io parser for clean text PDFs and Word documents.
Makes zero LLM calls — pure layout-aware text extraction.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.models import ParsedDocument, ParsedElement

logger = logging.getLogger(__name__)

# Unstructured element types we care about (maps to our own types)
_TYPE_MAP = {
    "Title": "Title",
    "Header": "Header",
    "NarrativeText": "NarrativeText",
    "Table": "Table",
    "ListItem": "ListItem",
    "FigureCaption": "NarrativeText",
    "Text": "NarrativeText",
    "UncategorizedText": "NarrativeText",
}


def parse(file_path: str | Path) -> ParsedDocument:
    """
    Parse a clean text PDF or Word document using Unstructured.io.
    Returns a ParsedDocument with structured elements in reading order.
    """
    from unstructured.partition.auto import partition
    from unstructured.documents.elements import Table, Title, Header

    path = Path(file_path)
    logger.info("Parsing %s with Unstructured.io", path.name)

    raw_elements = partition(
        filename=str(path),
        include_page_breaks=True,
        strategy="hi_res",
    )

    elements: list[ParsedElement] = []
    current_page = 1

    for elem in raw_elements:
        elem_type = type(elem).__name__

        # Track page number from PageBreak elements
        if elem_type == "PageBreak":
            current_page += 1
            continue

        text = str(elem).strip()
        if not text:
            continue

        mapped_type = _TYPE_MAP.get(elem_type, "NarrativeText")

        # Pull page number from element metadata if available
        page_num = current_page
        if hasattr(elem, "metadata") and hasattr(elem.metadata, "page_number"):
            page_num = elem.metadata.page_number or current_page

        elements.append(ParsedElement(
            element_type=mapped_type,
            text=text,
            page_number=page_num,
            metadata={"unstructured_type": elem_type},
        ))

    # Quality check — if almost no content was extracted, signal failure
    total_chars = sum(len(e.text) for e in elements)
    if total_chars < 200:
        raise ParseQualityError(
            f"Unstructured extracted only {total_chars} chars from {path.name} — "
            "consider vision LLM fallback"
        )

    logger.info(
        "Unstructured extracted %d elements (%d chars) from %s",
        len(elements), total_chars, path.name,
    )

    return ParsedDocument(
        source_file=path.name,
        parser_used="unstructured",
        elements=elements,
        total_pages=current_page,
    )


class ParseQualityError(RuntimeError):
    """Raised when extracted content is too sparse to be reliable."""
