# Tesseract OCR Parser — Design Spec

**Date:** 2026-07-01
**Status:** Draft — pending user review
**Context:** First live ingestion of scanned resolution PDFs produced unreliable data — fabricated vote rolls (every resolution came out as an identical 7-member all-"Yea" slate; the printed roll on Res 1 shows only 5 YEAS, and two DB names aren't on the page) and missing dollar amounts.

---

## 1. Problem & root cause

The resolution PDFs are **scanned images with no embedded text layer** — `pdfplumber`, `pypdf`, and `pdfminer` all extract ~0 characters; each page is a full-page image. (Text is selectable in a viewer only because the viewer OCRs live.)

The detector therefore classifies them as `complex_pdf` and routes them to the **Claude vision parser**, which reads each page **as an image** and performs transcription *and* interpretation in a single LLM call. That interpretation step is **hallucinating** — padding every resolution to a fixed 7/7 all-Yea roster regardless of the actual printed roll. It is also the largest cost line: **$4.60 (~56%) of all logged LLM spend**, at ~$0.017/page.

**Root fix:** separate faithful transcription from interpretation. Use a dedicated OCR engine (**Tesseract 5.5.2, already installed**) to produce plain text from the page images, then run the *existing, proven* text pipeline on that text. Tesseract only transcribes — it cannot invent content that isn't printed.

## 2. Goals

1. Add a Tesseract OCR parser that converts scanned/low-text PDFs to plain text.
2. Route low-text PDFs to Tesseract first, falling back to Claude vision when OCR output is poor — so scanned resolutions get OCR while graphical slide-deck quarterly reports (Public Works/DEDBH) keep vision.
3. Feed OCR text into the unchanged chunk → classify → extract → embed pipeline (keep-together chunking + anchored extraction already handle resolutions).
4. Validate by re-ingesting the 7 resolutions: vote counts should vary (not uniform 7/7) and amounts should appear.

## 3. Non-Goals

- No change to chunking or extraction code — OCR text flows through the existing clean-text path.
- No change to the clean-text (`unstructured`) path for native-text PDFs.
- Not removing the vision parser — it remains the fallback and the parser for graphical slide decks.
- No handwriting solution — printed content (resolution number, title, vendor, amount, YEAS/NAYS roll) is what matters and is printed; handwritten sponsor/second/date stamps are out of scope (the passage-date stamp may be lossy).

## 4. Design

### 4.1 Tesseract parser

`src/ingestion/parsers/tesseract_parser.py`:

```
parse(file_path, settings=None) -> ParsedDocument
```

- Render pages to images with `pdf2image.convert_from_path(path, dpi=cfg.ocr_dpi)` (same library the vision parser already uses).
- OCR each page image with Tesseract. **Default mechanism: shell out to the installed `tesseract` binary via `subprocess`** (zero new dependency — proven working). A thin `_ocr_image(image) -> str` seam isolates this so it can be mocked in tests (and swapped for `pytesseract` later if desired).
- Split each page's text into paragraph blocks on blank lines; emit one `ParsedElement(element_type="NarrativeText", text=block, page_number=n)` per block. Splitting on blank lines ensures clause markers (`WHEREAS`, `RESOLVED`, `YEAS`) land at element starts so keep-together chunking works.
- Return `ParsedDocument(source_file, parser_used="tesseract", elements, total_pages)`.
- Never raises for a single bad page: on a page OCR error, emit a placeholder element and continue (mirrors the vision parser's per-page resilience).

### 4.2 Routing with vision fallback

Modify the pipeline's parse step (`IngestionPipeline._parse_with_fallback(path, doc_kind)`):

- `clean_text_pdf` / `word_doc` → unchanged (unstructured, with existing vision fallback on `ParseQualityError`).
- `complex_pdf` → **try Tesseract first**:
  1. `parsed = tesseract_parser.parse(path, cfg)`
  2. If `ocr_quality_ok(parsed, cfg)` → use it.
  3. Else (sparse/garbled OCR — e.g. graphical slide decks) → `vision_parser.parse(path, cfg)` (fallback).
  4. If Tesseract itself errors → log + `vision_parser.parse(path, cfg)` (fallback).

`ocr_quality_ok(parsed, cfg) -> bool` (in `tesseract_parser.py` or `detector.py`):
- `chars_per_page = total_text_chars / max(1, total_pages)`
- returns `chars_per_page >= cfg.ocr_min_chars_per_page` AND `garbled_ratio(all_text) < cfg.garbled_ratio_threshold` (reuse `detector._garbled_ratio`).

This is outcome-based: scanned text docs yield dense clean text (Tesseract wins); graphical slide decks yield little/garbled text (fall back to vision). No need to know the document type before parsing.

### 4.3 Downstream — no changes

`parser_used="tesseract"` is **not** `"vision_llm"`, so `chunk_document` routes OCR text through the existing clean-text path: keep-together chunking when the registry supplies `keep_together` hints (resolutions), else section-aware chunking. Classification, anchored extraction (`extract_for_type` with the resolution `anchor_field`), embeddings, and storage are all unchanged. The reliability win comes entirely from extraction now operating on faithfully-transcribed text rather than an interpreted image.

### 4.4 Config (`src/config.py`)

- `ocr_dpi: int = 200` — render resolution for OCR.
- `ocr_min_chars_per_page: int = 150` — below this, OCR is considered poor → vision fallback.
- (reuses existing `garbled_ratio_threshold`.)

## 5. Error handling

- Per-page OCR failure → placeholder element, continue (no lost page, no crash).
- Whole-document Tesseract failure or poor-quality OCR → automatic Claude-vision fallback (behavior identical to today for those docs).
- The parse step never crashes ingestion; a document that fails both parsers surfaces as an ingestion error for that file only (existing per-document isolation).

## 6. Testing

- **Unit (`tests/ingestion/parsers/test_tesseract_parser.py`):**
  - `parse` with `_ocr_image` mocked to return known multi-paragraph text → asserts one `ParsedElement` per blank-line block, correct `page_number`, `parser_used="tesseract"`, and that `WHEREAS`/`RESOLVED` land at element starts.
  - Per-page error path → placeholder element emitted, no raise.
- **Unit — `ocr_quality_ok`:** dense clean text → True; sparse text (< threshold) → False; garbled text (> garbled threshold) → False.
- **Unit — routing (`tests/ingestion/test_parse_routing.py`):** with `tesseract_parser` and `vision_parser` stubbed, `_parse_with_fallback` on `complex_pdf` uses Tesseract when `ocr_quality_ok` is True, falls back to vision when False, and falls back to vision when Tesseract raises.
- **Integration (marked):** OCR a real resolution page fixture through Tesseract (the binary is installed) and assert non-trivial text is returned.
- **Operator validation (live, post-merge):** re-ingest the 7 resolutions; confirm (a) they route to `tesseract` (not `vision_llm`) in the `documents` table, (b) vote counts **vary across resolutions** (no longer a uniform 7/7 all-Yea), (c) amounts appear where the doc states them, (d) `ingestion.vision_parser` cost for these docs drops to ~$0.

## 7. Files

**New**
- `src/ingestion/parsers/tesseract_parser.py`
- `tests/ingestion/parsers/test_tesseract_parser.py` (+ `__init__.py` if needed)
- `tests/ingestion/test_parse_routing.py`

**Modified**
- `src/ingestion/pipeline.py` — `_parse_with_fallback` routes `complex_pdf` to Tesseract-first with vision fallback.
- `src/config.py` — `ocr_dpi`, `ocr_min_chars_per_page`.

**Unchanged (intentionally):** `chunker.py`, `sql_extractor.py`, `detector.py` (may add `ocr_quality_ok` helper here or in the parser), the vision and unstructured parsers.

## 8. Decisions locked with the user

| Decision | Choice |
|---|---|
| Approach | Tesseract OCR front-end → plain text → existing pipeline |
| Routing | Tesseract-first for low-text PDFs; vision fallback on poor OCR |
| Dependency | Shell out to installed `tesseract` binary (zero new deps); `pytesseract` optional later |
| Chunking/extraction | Unchanged (OCR text uses the clean-text path) |
| Handwriting | Out of scope (printed content is what matters) |
| Validation | Re-ingest the 7 resolutions; expect varied votes + recovered amounts |

## 9. Open items

- Exact `ocr_min_chars_per_page` threshold — start at 150, tune if a real slide deck is mistakenly kept on OCR or a real scanned doc wrongly falls back to vision.
- Whether to persist OCR text (e.g., cache) to avoid re-OCR on future re-ingests — deferred (OCR is cheap/local; YAGNI).
- Vote extraction accuracy on the clean text is expected to improve, but if the printed YEAS/NAYS structure still extracts imperfectly, a resolution-specific vote-parsing prompt is a possible follow-up (not in scope now).
