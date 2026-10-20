# Tesseract OCR Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Tesseract OCR parser that transcribes scanned/low-text PDFs to plain text, routed Tesseract-first with a Claude-vision fallback, so scanned resolutions get faithful text (no hallucinated votes) while graphical slide-deck QRs keep vision.

**Architecture:** A new `tesseract_parser` renders pages with `pdf2image` and OCRs each via the installed `tesseract` binary (subprocess, zero new deps), returning paragraph `ParsedElement`s with `parser_used="tesseract"`. The pipeline's `_parse_with_fallback` tries Tesseract first for `complex_pdf`, keeps the result if an OCR-quality check passes, else falls back to the vision parser. OCR text then flows through the **existing** clean-text chunking + anchored extraction unchanged.

**Tech Stack:** Python 3.11+, `pdf2image` (already installed) + `tesseract` 5.5.2 binary (already installed), `subprocess`, `pytest`.

## Global Constraints

- **Zero new dependencies** — OCR shells out to the installed `tesseract` binary via `subprocess`; do not add `pytesseract`/`ocrmypdf`.
- `parser_used` for OCR output MUST be `"tesseract"` (NOT `"vision_llm"`) so the existing chunker routes it through the clean-text/keep-together path, not slide-deck chunking.
- No changes to `chunker.py`, `sql_extractor.py`, the vision parser, or the unstructured parser. The clean-text path and vision path stay as-is; only `complex_pdf` routing changes.
- Parsing MUST NOT crash ingestion: a per-page OCR error → placeholder element + continue; a whole-doc Tesseract failure OR low-quality OCR → fall back to the vision parser (today's behavior for those docs).
- `ParsedElement` is a dataclass constructed positionally: `ParsedElement(element_type, text, page_number)`. `ParsedDocument(source_file, parser_used, elements, total_pages)`.
- Tests run with `pytest`; live-DB / real-OCR-on-corpus validation is an operator step (`-m integration` or the operator script), not CI.

---

### Task 1: Tesseract parser + config + OCR quality check

**Files:**
- Modify: `src/config.py` (add `ocr_dpi`, `ocr_min_chars_per_page`)
- Create: `src/ingestion/parsers/tesseract_parser.py`
- Test: `tests/ingestion/parsers/__init__.py`, `tests/ingestion/parsers/test_tesseract_parser.py`

**Interfaces:**
- Produces:
  - `Settings.ocr_dpi: int = 200`, `Settings.ocr_min_chars_per_page: int = 150`
  - `tesseract_parser.parse(file_path, settings=None) -> ParsedDocument` (`parser_used="tesseract"`)
  - `tesseract_parser._ocr_image(image) -> str` (single-page OCR seam, mockable)
  - `tesseract_parser._split_blocks(text) -> list[str]`
  - `tesseract_parser.ocr_quality_ok(parsed: ParsedDocument, settings=None) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/parsers/test_tesseract_parser.py
import types
from src.ingestion.parsers import tesseract_parser as tp
from src.models import ParsedDocument, ParsedElement

def test_split_blocks_on_blank_lines():
    text = "WHEREAS the city finds it necessary\n\nRESOLVED that it is authorized\n\n  \n"
    assert tp._split_blocks(text) == ["WHEREAS the city finds it necessary", "RESOLVED that it is authorized"]

def test_parse_builds_tesseract_elements(monkeypatch):
    # Fake 2 rendered pages; OCR returns known text per page.
    monkeypatch.setattr(tp.pdf2image, "convert_from_path", lambda *a, **k: ["img1", "img2"])
    pages = {"img1": "RESOLUTION NO. 9-2026\n\nWHEREAS the city ...", "img2": "YEAS\n\nMS. DAVIS"}
    monkeypatch.setattr(tp, "_ocr_image", lambda im: pages[im])
    doc = tp.parse("whatever.pdf")
    assert doc.parser_used == "tesseract"
    assert doc.total_pages == 2
    # blocks split; markers land at element starts
    texts = [e.text for e in doc.elements]
    assert texts[0].startswith("RESOLUTION NO. 9-2026")
    assert any(t.startswith("WHEREAS") for t in texts)
    assert any(t.startswith("YEAS") for t in texts)
    assert doc.elements[-1].page_number == 2

def test_parse_page_ocr_error_is_isolated(monkeypatch):
    monkeypatch.setattr(tp.pdf2image, "convert_from_path", lambda *a, **k: ["p1"])
    def boom(im): raise RuntimeError("tess crash")
    monkeypatch.setattr(tp, "_ocr_image", boom)
    doc = tp.parse("x.pdf")
    assert doc.parser_used == "tesseract" and len(doc.elements) == 1
    assert "OCR failed" in doc.elements[0].text   # placeholder, no raise

def _doc(text, pages):
    return ParsedDocument(source_file="x", parser_used="tesseract",
                          elements=[ParsedElement("NarrativeText", text, 1)], total_pages=pages)

def test_ocr_quality_ok_dense_text_true():
    assert tp.ocr_quality_ok(_doc("A" * 400, pages=1)) is True

def test_ocr_quality_ok_sparse_text_false():
    assert tp.ocr_quality_ok(_doc("short", pages=1)) is False    # < 150 chars/page
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/parsers/test_tesseract_parser.py -v`
Expected: FAIL (`ModuleNotFoundError: src.ingestion.parsers.tesseract_parser`)

- [ ] **Step 3: Add config settings to `src/config.py`**

Inside `Settings` (near the other ingestion settings):

```python
    # Tesseract OCR (scanned/low-text PDFs)
    ocr_dpi: int = 200                    # render resolution for OCR
    ocr_min_chars_per_page: int = 150     # below this, OCR is poor -> fall back to Vision LLM
```

- [ ] **Step 4: Create `src/ingestion/parsers/tesseract_parser.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/ingestion/parsers/test_tesseract_parser.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Run the full unit suite**

Run: `pytest -q -m "not integration"`
Expected: green (no regressions)

- [ ] **Step 7: Commit**

```bash
git add src/config.py src/ingestion/parsers/tesseract_parser.py tests/ingestion/parsers/
git commit -m "feat(ingestion): add Tesseract OCR parser + OCR-quality check"
```

---

### Task 2: Route complex_pdf to Tesseract-first with vision fallback

**Files:**
- Modify: `src/ingestion/pipeline.py` (`_parse_with_fallback`)
- Test: `tests/ingestion/test_parse_routing.py`

**Interfaces:**
- Consumes: `tesseract_parser.parse` / `tesseract_parser.ocr_quality_ok` (Task 1); existing `vision_parser.parse`, `unstructured_parser.parse`.
- Produces: `IngestionPipeline._parse_with_fallback(path, doc_kind)` now, for `complex_pdf`, returns the Tesseract result when `ocr_quality_ok`, else the vision result; Tesseract exceptions fall back to vision. `clean_text_pdf`/`word_doc` paths unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_parse_routing.py
from pathlib import Path
from src.ingestion import pipeline as P
from src.models import ParsedDocument

def _pipe():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    from src.config import get_settings
    pipe.cfg = get_settings()
    return pipe

def _tess_doc(): return ParsedDocument("f.pdf", "tesseract", [], 3)
def _vis_doc():  return ParsedDocument("f.pdf", "vision_llm", [], 3)

def test_complex_pdf_uses_tesseract_when_quality_ok(monkeypatch):
    monkeypatch.setattr(P.tesseract_parser, "parse", lambda p, c: _tess_doc())
    monkeypatch.setattr(P.tesseract_parser, "ocr_quality_ok", lambda d, c: True)
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: (_ for _ in ()).throw(AssertionError("vision should not run")))
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "complex_pdf")
    assert out.parser_used == "tesseract"

def test_complex_pdf_falls_back_to_vision_when_quality_poor(monkeypatch):
    monkeypatch.setattr(P.tesseract_parser, "parse", lambda p, c: _tess_doc())
    monkeypatch.setattr(P.tesseract_parser, "ocr_quality_ok", lambda d, c: False)
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: _vis_doc())
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "complex_pdf")
    assert out.parser_used == "vision_llm"

def test_complex_pdf_falls_back_to_vision_when_tesseract_raises(monkeypatch):
    def boom(p, c): raise RuntimeError("tess down")
    monkeypatch.setattr(P.tesseract_parser, "parse", boom)
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: _vis_doc())
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "complex_pdf")
    assert out.parser_used == "vision_llm"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_parse_routing.py -v`
Expected: FAIL (`P.tesseract_parser` not imported; complex_pdf still goes straight to vision)

- [ ] **Step 3: Edit `src/ingestion/pipeline.py`**

Add the import alongside the other parser imports:

```python
from src.ingestion.parsers import tesseract_parser
```

Replace the `complex_pdf` branch of `_parse_with_fallback`:

```python
    def _parse_with_fallback(self, path: Path, doc_kind: str):
        """complex_pdf: Tesseract OCR first, fall back to Vision LLM on poor/failed OCR.
        clean_text/word: Unstructured, with Vision fallback on quality failure."""
        if doc_kind == "complex_pdf":
            try:
                parsed = tesseract_parser.parse(path, self.cfg)
                if tesseract_parser.ocr_quality_ok(parsed, self.cfg):
                    return parsed
                logger.info("OCR quality low for %s — falling back to Vision LLM", path.name)
            except Exception as e:
                logger.warning("Tesseract failed for %s: %s — falling back to Vision LLM", path.name, e)
            return vision_parser.parse(path, self.cfg)

        if doc_kind in ("clean_text_pdf", "word_doc"):
            try:
                return unstructured_parser.parse(path)
            except ParseQualityError as e:
                logger.warning("Unstructured quality check failed: %s — retrying with Vision LLM", e)
                return vision_parser.parse(path, self.cfg)

        raise ValueError(f"Unsupported document kind: {doc_kind}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_parse_routing.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Full suite + clean import**

Run: `pytest -q -m "not integration"` then `python3 -c "import src.ingestion.pipeline"`
Expected: green; import clean.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/pipeline.py tests/ingestion/test_parse_routing.py
git commit -m "feat(ingestion): route complex PDFs to Tesseract-first with Vision fallback"
```

---

### Task 3: Operator validation — re-ingest the 7 resolutions

**Files:**
- Create: `scripts/reingest_resolutions.py` (a convenience validator; operator-run)

**Interfaces:**
- Consumes: `IngestionPipeline` (Tasks 1–2). No unit test — this is live-DB validation.

- [ ] **Step 1: Write the validator script**

```python
# scripts/reingest_resolutions.py
"""Re-ingest the resolution PDFs through the Tesseract-first pipeline and validate the data.
Operator-run (needs live Supabase/Neo4j + tesseract). Usage: python3 scripts/reingest_resolutions.py"""
import glob, os, logging, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
logging.basicConfig(level=logging.ERROR)
from src.config import get_settings
from src.ingestion.pipeline import IngestionPipeline
from src.storage.sql_store import SQLStore

def main():
    cfg = get_settings()
    pipe = IngestionPipeline(cfg); pipe.initialize_stores()
    files = [f for f in sorted(glob.glob("docs/Resolutions*.pdf")) if "(1)" not in f]
    print(f"Re-ingesting {len(files)} resolutions (Tesseract-first)...")
    for f in files:
        pipe.ingest_document(f)
        print("  ingested", os.path.basename(f))
    s = SQLStore(cfg); s.connect()
    with s.cursor() as cur:
        cur.execute("SELECT DISTINCT parser_used FROM documents WHERE source_file ILIKE 'Resolutions%'")
        print("parser_used for resolutions:", [r["parser_used"] for r in cur.fetchall()])
        cur.execute("SELECT resolution_number, count(*) FROM votes GROUP BY resolution_number ORDER BY resolution_number")
        print("votes per resolution (expect VARIED, not all 7):")
        for r in cur.fetchall(): print("   ", r["resolution_number"], "->", r["count"])
        cur.execute("SELECT count(*) AS c FROM resolutions WHERE amount IS NOT NULL")
        print("resolutions with an amount:", cur.fetchone()["c"])
    s.close()

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it (operator, live env)**

Run: `python3 scripts/reingest_resolutions.py`
Expected:
- `parser_used for resolutions: ['tesseract']` (Tesseract handled them, not vision),
- **votes per resolution VARY** (e.g. Res 1 → 5, others differ) — no longer a uniform 7,
- more resolutions now carry an amount than before.

- [ ] **Step 3: Spot-check against the printed page**

For Resolution 1, confirm the extracted YEAS match the printed roll (Davis, Green, Jones, Lawson, Rawls) and that Rodriguez/Hill are gone.

- [ ] **Step 4: Commit the script**

```bash
git add scripts/reingest_resolutions.py
git commit -m "chore(ingestion): resolution re-ingest + OCR validation script"
```

---

## Self-Review

**Spec coverage:**
- §4.1 Tesseract parser (render, subprocess OCR seam, blank-line block split, `parser_used="tesseract"`, per-page resilience) → Task 1. ✓
- §4.2 routing Tesseract-first + `ocr_quality_ok` + vision fallback (poor OR error) → Task 1 (`ocr_quality_ok`) + Task 2 (routing). ✓
- §4.3 downstream unchanged → guaranteed by `parser_used="tesseract"` (Global Constraints) + no chunker/extractor edits. ✓
- §4.4 config `ocr_dpi`, `ocr_min_chars_per_page` → Task 1. ✓
- §5 error handling (per-page placeholder, whole-doc fallback, never crash) → Task 1 (page) + Task 2 (doc). ✓
- §6 testing (parser split/elements, page-error isolation, quality heuristic, routing three-way, operator re-ingest) → Tasks 1–3. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. The real-OCR integration test is folded into the operator validation (Task 3) rather than a flaky CI fixture — explicit, not a gap.

**Type consistency:** `parse(file_path, settings=None)` / `ocr_quality_ok(parsed, settings=None)` / `_ocr_image` / `_split_blocks` are defined in Task 1 and referenced with matching signatures in Task 2 (`tesseract_parser.parse(path, self.cfg)`, `ocr_quality_ok(parsed, self.cfg)`). `ParsedElement(element_type, text, page_number)` and `ParsedDocument(source_file, parser_used, elements, total_pages)` match the dataclasses. `_garbled_ratio` imported from `detector`. The pipeline references `P.tesseract_parser` (the import added in Task 2 Step 3) — the routing tests monkeypatch that name.

**Note:** create `tests/ingestion/parsers/__init__.py` (Task 1) so the new test package is discovered.
