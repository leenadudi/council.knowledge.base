"""Document type detection — determines which parser to use."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

import pdfplumber

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

DocumentKind = Literal["clean_text_pdf", "complex_pdf", "word_doc", "other"]


def detect(file_path: str | Path, settings: Settings | None = None) -> DocumentKind:
    """
    Determine how to parse a document.

    Returns one of:
      - clean_text_pdf  → use Unstructured.io parser
      - complex_pdf     → use Vision LLM parser (slide decks, dark backgrounds)
      - word_doc        → use Unstructured.io parser
      - other           → unsupported, skip
    """
    cfg = settings or get_settings()
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix in (".docx", ".doc"):
        return "word_doc"

    if suffix not in (".pdf",):
        return "other"

    # --- PDF analysis ---
    try:
        with pdfplumber.open(path) as pdf:
            total_pages = len(pdf.pages)
            if total_pages == 0:
                return "other"

            sample_pages = pdf.pages[:min(5, total_pages)]
            total_chars = sum(len(p.extract_text() or "") for p in sample_pages)
            chars_per_page = total_chars / len(sample_pages)

            # Very little text per page → likely a slide deck or image-heavy doc
            if chars_per_page < cfg.text_per_page_threshold:
                logger.info("%s → complex_pdf (%.0f chars/page)", path.name, chars_per_page)
                return "complex_pdf"

            # Check garbled character ratio on first page
            first_text = pdf.pages[0].extract_text() or ""
            garbled_ratio = _garbled_ratio(first_text)
            if garbled_ratio > cfg.garbled_ratio_threshold:
                logger.info("%s → complex_pdf (garbled ratio %.2f)", path.name, garbled_ratio)
                return "complex_pdf"

    except Exception as e:
        logger.warning("pdfplumber failed on %s: %s — defaulting to complex_pdf", path.name, e)
        return "complex_pdf"

    logger.info("%s → clean_text_pdf", path.name)
    return "clean_text_pdf"


def _garbled_ratio(text: str) -> float:
    """Fraction of characters that look like garbled extraction artifacts."""
    if not text:
        return 0.0
    garbled = len(re.findall(r"[^\x00-\x7F -⁯‘-”]", text))
    return garbled / len(text)
