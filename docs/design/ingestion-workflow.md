# Document Ingestion Workflow

How each type of document becomes structured data. Generated 2026-07-10 from
`src/ingestion/pipeline.py`, `registry.py`, `models.py`, and `extraction/sql_extractor.py`.

## Shared front end (every document)

`IngestionPipeline.ingest_document()` runs these stages for **all** documents, in order:

1. **Detect** — `detector.py`: pdfplumber inspects extension, chars-per-page, and garbled
   ratio → `clean_text_pdf` | `complex_pdf` | `word_doc` | `other`. Picks the parser.
2. **Parse** — `_parse_with_fallback`:
   - `complex_pdf` → Tesseract OCR → *(low quality)* Vision LLM
   - `clean_text_pdf` / `word_doc` → Unstructured → *(quality fail)* Vision LLM
   - garbled text triggers a one-shot Vision re-read (`_escalate_if_garbled`)
3. **Profile** — `profiler.py` (Haiku): agentic read of the first pages → `document_type`,
   `department`, `period`, `identifying_ids`, and a `confidence` score.
4. **Quarantine gate** — `_is_quarantined`: type is `unclassified`, not registered, or
   `confidence < 0.55` → **vector-store only** + a `review_flags` row. Stops here.
5. **Chunk** — `chunker.py`, using the document type's `ChunkingHints`.
6. **Classify content-type** — `classifier.py` (LLM), constrained to the type's `content_vocab`.
7. **Embed** — Voyage `voyage-3`, 1024-d, batched.
8. **Store & route** — vector store **always**; SQL + graph only if not quarantined, per the
   type's declared targets (below). Then `record_document` writes the `documents` row.

What differs by type is the **chunking hint**, the **content vocab**, and the **extraction
path**. Registered in `src/ingestion/registry.py`.

---

## `quarterly_report` — 63 docs

- **Chunking:** default section-aware (`_chunk_by_sections`)
- **Content vocab:** narrative · table · metrics · org_data · project · header
- **Extraction:** special-cased branch in `pipeline.py` (NOT the schema path). Three sub-paths:
  - **Generic batched** over `routes_to_sql()` chunks → `expenditures`, `metrics`
    ⚠️ *fragile: only sees chunks classified `table`/`metrics`; drops the rest*
  - **Keyword-selected** focused extractors → `goals` (`"goal"`), `grants` (`"grant"`),
    `vacancies` (`"vacan"`) ✓ *robust; bypasses the gate*
  - **Graph** → Department / Person / Project / Grant nodes
- **Feeds:** `expenditures`, `metrics`, `grants`, `goals`, `vacancies`

## `resolution` — 21 docs

- **Chunking:** `keep_together = [whereas, resolved]` — legal clauses stay in one chunk
- **Content vocab:** legal_authorization · whereas_clause · vote_record · narrative · header
- **Extraction:** `extract_for_type` against `ResolutionExtraction`; **anchored on
  `resolution_number`** (collapses to exactly one primary record, kills hallucinated duplicates)
- **Feeds:** `resolutions` (1 per doc), `votes`
- **Graph:** Resolution / Vendor / CouncilMember

## `minutes` — 11 docs

- **Chunking:** default section-aware
- **Content vocab:** narrative · roll_call · agenda_action · header
- **Extraction:** focused `extract_meeting` — one meeting record + one action row per
  resolution/ordinance acted on
- **Feeds:** `meetings` (1 per doc), `meeting_actions`

## `legislation` — 2 docs

- **Chunking:** `keep_together = [ordained, section]`
- **Content vocab:** legal_authorization · ordinance_clause · narrative · header
- **Extraction:** `extract_for_type` against `LegislationExtraction`; **anchored on `bill_number`**
- **Feeds:** `legislation` (1 per doc)

## `budget` — 15 docs

- **Chunking:** default section-aware
- **Content vocab:** table · narrative · metrics · header
- **Extraction:** `extract_for_type` against `BudgetExtraction` — **only if the doc is an
  annual/approved/proposed budget** (title/filename check). Bureau presentations & budget
  Q&A short-circuit to **searchable-only** (vector store only, no appropriations).
- **Feeds:** `appropriations` (3 of 15 docs)

## `« quarantine »` — low-confidence / unknown

- Triggered when type is `unclassified`, not registered, or `confidence < 0.55`.
- **No structured extraction.** Chunks embedded → vector store only, `needs_review=True`,
  plus a `review_flags` row for a human to reclassify.

---

## The one asymmetry to remember

`resolution` / `minutes` / `legislation` / `budget` all run the clean, deterministic
**schema path** (`extract_for_type` / focused extractors against a Pydantic schema).
`quarterly_report` alone runs the older special-cased branch with the fragile
`routes_to_sql()` content-type gate — which is why it is the only type that has had
silent extraction gaps (vacancies fixed 2026-07-10; `expenditures` and `metrics` still
ride the gate).
