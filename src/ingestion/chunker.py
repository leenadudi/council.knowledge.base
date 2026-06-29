"""
Section-aware chunking for quarterly reports.
Chunks at section header boundaries, keeps tables whole, applies overlap between narrative chunks.
"""

from __future__ import annotations

import logging
import re

from src.config import Settings, get_settings
from src.models import ParsedDocument, ParsedElement

logger = logging.getLogger(__name__)

# Known quarterly report section names (case-insensitive, partial match)
_SECTION_HEADERS = [
    "description", "background", "mission",
    "quarterly summary", "executive summary",
    "metrics", "counts", "performance indicators", "statistics",
    "budget", "expenditure", "financial",
    "annual goal", "goals and objectives",
    "special project", "capital project",
    "vacancy", "staffing", "personnel", "positions",
    "community engagement", "outreach",
    "grant", "funding",
]

_SECTION_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(h) for h in _SECTION_HEADERS) + r")\b"
)


def _is_section_header(element: ParsedElement) -> bool:
    """Return True if this element marks a new section boundary."""
    return element.element_type in ("Title", "Header") or bool(
        _SECTION_RE.search(element.text) and len(element.text) < 120
    )


class _ChunkBuffer:
    """Accumulates text until a section boundary is hit."""

    def __init__(self, section: str, page: int):
        self.section = section
        self.page = page
        self.parts: list[str] = []
        self.element_types: list[str] = []

    def add(self, text: str, element_type: str) -> None:
        self.parts.append(text)
        self.element_types.append(element_type)

    @property
    def text(self) -> str:
        return "\n\n".join(self.parts)

    @property
    def dominant_type(self) -> str:
        """Heuristic element type label for the accumulated content."""
        if "Table" in self.element_types:
            return "Table"
        if "OrgData" in self.element_types:
            return "OrgData"
        return self.element_types[0] if self.element_types else "NarrativeText"


def chunk_document(
    parsed: ParsedDocument,
    settings: Settings | None = None,
    hints=None,
) -> list[dict]:
    """
    Split a ParsedDocument into chunks following the spec rules.

    Returns a list of raw chunk dicts:
      {text, section, page_number, element_type, parser_used}

    When ``hints.keep_together`` is set, elements whose text begins with any
    marker word are accumulated into a single buffer that is only flushed when
    a *new* opening-marker block starts after the concluding marker has already
    been seen.  This keeps WHEREAS reasoning and RESOLVED conclusions together.
    """
    cfg = settings or get_settings()
    max_size = cfg.max_chunk_size
    min_size = cfg.min_chunk_size
    overlap = cfg.chunk_overlap

    # Slide deck: each element is its own natural chunk (one slide = one chunk)
    if parsed.parser_used == "vision_llm":
        return _chunk_slide_deck(parsed, min_size)

    # Registry-supplied keep-together hints: do not split marker blocks
    if hints and hints.keep_together:
        return _chunk_keep_together(parsed, cfg, hints.keep_together)

    # Clean text PDF: chunk at section boundaries
    return _chunk_by_sections(parsed, max_size, min_size, overlap)


def _chunk_keep_together(parsed: ParsedDocument, cfg, markers: list[str]) -> list[dict]:
    """
    Accumulate elements into one buffer, flushing only at a clean resolution
    boundary.

    Boundary rule (chosen to keep WHEREAS+RESOLVED intact):
    - The first marker in the list is the "opening" marker (e.g. "whereas").
    - The last marker in the list is the "concluding" marker (e.g. "resolved").
    - We flush the current buffer and start a new one only when an element
      begins with the opening marker AND the current buffer has already
      absorbed an element that begins with the concluding marker.
    - Elements that do not start any marker are appended to the current buffer.
    - Chunks below cfg.min_chunk_size are silently dropped (same as other paths).

    For a single WHEREAS->RESOLVED pair this yields one chunk.
    For a multi-resolution document each resolution gets its own chunk.
    """
    markers_lc = [m.lower() for m in markers]
    opening_marker = markers_lc[0]   # e.g. "whereas"
    closing_marker = markers_lc[-1]  # e.g. "resolved"
    chunks: list[dict] = []
    buf: _ChunkBuffer | None = None
    buf_has_closing = False  # True once the closing marker has been added to buf

    def _starts_marker(text: str, marker: str) -> bool:
        return text[:60].lower().lstrip().startswith(marker)

    def _flush(b: _ChunkBuffer | None) -> None:
        if b and b.parts and len(b.text) >= cfg.min_chunk_size:
            chunks.append({
                "text": b.text,
                "section": b.section,
                "page_number": b.page,
                "element_type": b.dominant_type,
                "parser_used": parsed.parser_used,
            })

    for elem in parsed.elements:
        is_opening = _starts_marker(elem.text, opening_marker)
        is_closing = _starts_marker(elem.text, closing_marker)

        if is_opening and buf is not None and buf_has_closing:
            # A fresh resolution begins after a prior one was fully closed --
            # flush the completed buffer and start a new one.
            _flush(buf)
            buf = _ChunkBuffer(elem.text.strip()[:80], elem.page_number)
            buf_has_closing = False
            buf.add(elem.text, elem.element_type)
        elif buf is None:
            # First element -- start the initial buffer
            buf = _ChunkBuffer(elem.text.strip()[:80], elem.page_number)
            buf_has_closing = is_closing
            buf.add(elem.text, elem.element_type)
        else:
            # Continue accumulating into the current buffer
            buf.add(elem.text, elem.element_type)
            if is_closing:
                buf_has_closing = True

    _flush(buf)
    return chunks


def _chunk_slide_deck(parsed: ParsedDocument, min_size: int) -> list[dict]:
    """For slide deck PDFs: group elements by page, each page = one chunk."""
    by_page: dict[int, list[ParsedElement]] = {}
    for elem in parsed.elements:
        by_page.setdefault(elem.page_number, []).append(elem)

    chunks = []
    for page_num in sorted(by_page):
        elements = by_page[page_num]
        text = "\n\n".join(e.text for e in elements).strip()
        if len(text) < min_size:
            continue
        # Determine section from first Title/Header on this page
        section = next(
            (e.text[:80] for e in elements if e.element_type in ("Title", "Header")),
            f"Page {page_num}",
        )
        chunks.append({
            "text": text,
            "section": section,
            "page_number": page_num,
            "element_type": _dominant_type(elements),
            "parser_used": parsed.parser_used,
        })
    return chunks


def _chunk_by_sections(
    parsed: ParsedDocument,
    max_size: int,
    min_size: int,
    overlap: int,
) -> list[dict]:
    """
    Chunk clean-text documents at section header boundaries.
    Tables are never split. Narrative chunks exceeding max_size are split at paragraph boundaries.
    """
    chunks: list[dict] = []
    current_section = "Introduction"
    current_page = 1
    buffer = _ChunkBuffer(current_section, current_page)

    def flush(buf: _ChunkBuffer, prev_tail: str = "") -> str:
        """Flush the buffer to chunks. Returns the tail text for overlap."""
        text = (prev_tail + "\n\n" + buf.text).strip() if prev_tail else buf.text.strip()
        if len(text) < min_size:
            return ""

        if buf.dominant_type == "Table" or len(text) <= max_size:
            chunks.append({
                "text": text,
                "section": buf.section,
                "page_number": buf.page,
                "element_type": buf.dominant_type,
                "parser_used": parsed.parser_used,
            })
            return text[-overlap:] if len(text) > overlap else ""

        # Split large narrative at paragraph boundaries
        paragraphs = text.split("\n\n")
        sub = ""
        for para in paragraphs:
            candidate = (sub + "\n\n" + para).strip() if sub else para.strip()
            if len(candidate) > max_size and sub:
                chunks.append({
                    "text": sub,
                    "section": buf.section,
                    "page_number": buf.page,
                    "element_type": buf.dominant_type,
                    "parser_used": parsed.parser_used,
                })
                sub = sub[-overlap:] + "\n\n" + para if len(sub) > overlap else para
            else:
                sub = candidate
        if sub and len(sub) >= min_size:
            chunks.append({
                "text": sub,
                "section": buf.section,
                "page_number": buf.page,
                "element_type": buf.dominant_type,
                "parser_used": parsed.parser_used,
            })
        return sub[-overlap:] if len(sub) > overlap else ""

    prev_tail = ""

    for elem in parsed.elements:
        current_page = elem.page_number

        if _is_section_header(elem):
            if buffer.parts:
                prev_tail = flush(buffer, prev_tail)
            current_section = elem.text.strip()[:80]
            buffer = _ChunkBuffer(current_section, current_page)
            # Include the header text in the new section's first chunk
            buffer.add(elem.text, elem.element_type)
        elif elem.element_type == "Table":
            # Tables always get their own chunk
            if buffer.parts:
                prev_tail = flush(buffer, prev_tail)
                buffer = _ChunkBuffer(current_section, current_page)
            table_chunk = _ChunkBuffer(current_section, current_page)
            table_chunk.add(elem.text, "Table")
            flush(table_chunk, "")
        else:
            buffer.add(elem.text, elem.element_type)

    if buffer.parts:
        flush(buffer, prev_tail)

    return chunks


def _dominant_type(elements: list[ParsedElement]) -> str:
    types = [e.element_type for e in elements]
    if "Table" in types:
        return "Table"
    if "OrgData" in types:
        return "OrgData"
    return types[0] if types else "NarrativeText"
