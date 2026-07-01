"""Tesseract OCR parser: scanned/image PDFs -> plain text via the local tesseract binary.
Transcription only (no interpretation), so it cannot hallucinate content the image lacks."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import pdf2image

from src.config import Settings, get_settings
from src.ingestion.detector import _garbled_ratio
from src.models import ParsedDocument, ParsedElement

logger = logging.getLogger(__name__)


def _ocr_image(image) -> str:
    """OCR a single rendered page via the installed `tesseract` binary.
    Isolated so tests can mock it. Writes a temp PNG (matches the proven CLI invocation)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = tf.name
        image.save(tmp, format="PNG")
    try:
        proc = subprocess.run(["tesseract", tmp, "stdout"], capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"tesseract exit {proc.returncode}: {proc.stderr.decode('utf-8','replace')[:200]}")
        return proc.stdout.decode("utf-8", "replace")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _split_blocks(text: str) -> list[str]:
    """Split a page's OCR text into paragraph blocks on blank lines; drop empties.
    Blank-line splitting keeps clause markers (WHEREAS/RESOLVED/YEAS) at block starts."""
    return [b.strip() for b in re.split(r"\n\s*\n", text or "") if b.strip()]


def parse(file_path: str | Path, settings: Optional[Settings] = None) -> ParsedDocument:
    cfg = settings or get_settings()
    path = Path(file_path)
    logger.info("Parsing %s with Tesseract OCR (dpi=%d)", path.name, cfg.ocr_dpi)
    images = pdf2image.convert_from_path(str(path), dpi=cfg.ocr_dpi)

    elements: list[ParsedElement] = []
    for page_idx, image in enumerate(images):
        page_num = page_idx + 1
        try:
            text = _ocr_image(image)
        except Exception as e:
            logger.warning("Tesseract failed on page %d of %s: %s", page_num, path.name, e)
            elements.append(ParsedElement("NarrativeText", f"[Page {page_num} — OCR failed: {e}]", page_num))
            continue
        for block in _split_blocks(text):
            elements.append(ParsedElement("NarrativeText", block, page_num))

    logger.info("Tesseract extracted %d elements from %d pages of %s", len(elements), len(images), path.name)
    return ParsedDocument(source_file=path.name, parser_used="tesseract", elements=elements, total_pages=len(images))


def ocr_quality_ok(parsed: ParsedDocument, settings: Optional[Settings] = None) -> bool:
    """True when OCR yielded enough clean text to trust; else the caller should fall back to Vision."""
    cfg = settings or get_settings()
    text = "\n".join(e.text for e in parsed.elements)
    chars_per_page = len(text) / max(1, parsed.total_pages)
    return chars_per_page >= cfg.ocr_min_chars_per_page and _garbled_ratio(text) < cfg.garbled_ratio_threshold
