# Agentic, Multi-Type Ingestion — Design Spec

**Date:** 2026-06-29
**Status:** Draft — pending user review
**Author:** Worked out with the city clerk's build priorities in mind (see `harrisburg_clerk_meeting_2026-06-29_buildnotes.md`)

---

## 1. Problem

The current ingestion pipeline is hardcoded to one document type — quarterly reports:

- `document_type` is a literal `"quarterly_report"` (`pipeline.py:119`, `metadata.py:83`).
- Department, quarter, and year are scraped from the **filename** via regex (`metadata.py`), which only works for the `Misc. Documents - Quarterly Reports - YYYY - Dept_QN YYYY.pdf` naming convention and is meaningless for a resolution, contract, or set of minutes.
- Content types are a fixed 6-value enum with fixed store-routing (`models.py:17`).

To support the clerk's priorities (spending authorization, grant tracking, etc.) the system must ingest **resolutions, ordinances, contracts, minutes, grant award letters, budget documents** — and adding each one today would mean hand-writing new chunking rules, content types, and schema wiring inside pipeline code.

A second, separate problem: documents are ingested **one at a time, sequentially** (`pipeline.py:59`), so loading or re-loading the corpus is slow.

## 2. Goals

1. **Schema-guided agentic ingestion.** An agent reads each document, identifies what it is (type, owner/department, date/period), and extracts structured data against a **declared, extensible registry** of document types. Adding a new type becomes a data change (a registry entry + its schemas), not a pipeline-code change.
2. **Content-derived metadata.** Replace filename-regex metadata with metadata the agent reads from the document itself.
3. **First new type end-to-end:** Resolutions — because they unlock the spending/authorization dashboard work and exercise SQL + graph + vote records, properly stress-testing the registry.
4. **Faster corpus ingestion** via bounded parallelism (load many documents at once instead of single-file).
5. **A testing approach** built on a golden set of real example documents.

## 3. Non-Goals

- Building every future document type now. Ordinances, contracts, minutes, grant award letters, and budget documents are **out of scope for this build** — they become registry entries in later work, once the framework is proven on quarterly reports + resolutions.
- Changing the query/retrieval path. This spec is ingestion-only. Query cost and latency are unaffected.
- Auto-creating database/graph schema for unrecognized document types (see Quarantine, §4.4).

## 4. Design

### 4.1 New per-document flow

```
detect parser kind        (unchanged: clean_text / complex / word)
   ↓
parse to elements         (unchanged)
   ↓
★ PROFILE  (agentic, NEW) → DocumentProfile:
                            { document_type, department/owner, period/date,
                              title, identifying_ids, confidence }
                            replaces filename-regex metadata
   ↓
★ REGISTRY LOOKUP (NEW)  → fetch the DocumentType spec for that type
                            (low confidence / unknown → QUARANTINE, §4.4)
   ↓
chunk                     using the type's chunking hints
   ↓
classify chunk            against the type's content vocabulary
   ↓
★ EXTRACT (agentic)      → extract structured data against the type's schema
   ↓
embed + store             vector always; SQL/graph per the type's targets
```

The two genuinely new steps are **Profile** and **Registry lookup**. Chunking, classification, and extraction already exist; they get *parameterized by the registry* instead of hardcoded to quarterly reports. Extraction and content classification are already LLM-driven today — this design pushes the LLM up one level rather than introducing it where there was none.

### 4.2 The Document Type Registry (the core new abstraction)

Each document type is declared **as data**, in one place (`src/ingestion/registry.py`):

```python
DocumentType(
  name="resolution",
  description="Formal council action authorizing a contract/expenditure/policy",  # profiler uses this to classify
  identifying_signals=["RESOLUTION NO", "WHEREAS", "RESOLVED"],
  metadata_schema=ResolutionMeta,          # res_no, adopted_date, amount, vendor, dept, status
  chunking=ChunkingHints(keep_together=["whereas", "resolved"]),
  content_vocab=["legal_authorization", "whereas_clause", "vote_record", ...],
  sql_targets=["resolutions", "votes"],
  graph_targets=["Resolution", "Vendor", "CouncilMember"],
  extraction_schema=ResolutionExtraction,  # Pydantic — doubles as the LLM extraction contract
)
```

**Adding a future type = add one `DocumentType` + its Pydantic schemas + any new SQL tables / graph nodes. No edits to `pipeline.py`, `chunker.py`, or `classifier.py`.** The Pydantic `extraction_schema` is handed to the LLM as the extraction contract, so the agent cannot drift off-schema (no `grant_match` vs `matching_funds` vs `local_share` inconsistency across documents).

**Registry lives as Python/Pydantic** (not YAML/JSON), because each type's schema needs validation logic that a flat config file can't carry.

### 4.3 Content-derived metadata

`document_type`, `department`/owner, and period now come from the **DocumentProfile** produced by the agent, not from the filename. The filename is kept as `source_file` and passed to the profiler only as a *weak hint*.

**Consequence:** existing quarterly reports are **re-ingested** through the new path so their metadata is content-derived. Output should match or improve on today's regex result; the existing query eval suite is the regression guard (§4.6).

### 4.4 Unknown / low-confidence documents — quarantine, don't guess

If the profiler cannot confidently classify a document (confidence below a configurable threshold), or it proposes a brand-new type:

- Store the document in the **vector store only**, tagged `document_type="unclassified"`, `needs_review=true`.
- Record it on a **review list** so a human can see it.
- The agent **may suggest** a new type name and candidate fields in the profile, but the system **never auto-creates SQL/graph schema**.

This keeps the document searchable without polluting the structured stores. A human later promotes it to a real registry entry.

### 4.5 Parallel (bounded-concurrency) ingestion

`ingest_directory` currently loops documents one at a time (`pipeline.py:59`). Replace with a **bounded worker pool**: a fixed number of workers (default configurable, e.g. 5) share the queue of documents; each processes one document end-to-end and pulls the next when free.

- Documents are independent, so this is safe with no shared-state changes.
- **Bounded** so we stay under Anthropic/Voyage rate limits. The worker count is a configurable dial.
- Most per-document time is spent *waiting* on API/DB calls, so concurrency fills idle wait-time — the work is I/O-bound, which is why a thread pool fits without an async rewrite.
- **Failure isolation is preserved:** one bad document logs and is skipped; other workers continue (matches today's per-document try/except).
- **Rate-limit handling:** a worker that receives a rate-limit (429) backs off and retries rather than failing.
- Secondary, optional win: make `classify_batch` (`classifier.py:80`, currently a serial list comprehension over ambiguous chunks) run its LLM calls concurrently too. Primary win is document-level concurrency; this is a follow-on within the same build.

**Note on latency:** a *single* document gets marginally slower (one added profiler call). A *full-corpus* ingest/re-ingest gets substantially faster because of the parallelism. The agentic redesign does not itself speed ingestion — the worker pool does.

### 4.6 Testing — golden set of real examples

The user provides a few real documents per type; these become fixtures in `tests/fixtures/`, each with a sidecar `expected.json`. Verification is **hybrid**:

- **Hard assertions (exact match):**
  - Profiler classifies the document as the expected `document_type`.
  - Critical scalar facts match exactly: dollar amounts, dates, resolution numbers, vote tallies, department/owner.
- **LLM-as-judge (reuse the `evaluation/evaluator.py` pattern):** scores doc-type rationale, categorization sanity, extraction completeness, and hallucination — fails below a threshold.
- **Regression gate:** the existing quarterly-report query eval suite must still pass after re-ingestion — proving the agentic rewrite did not degrade existing answers.

Test tiers: profiler unit tests, registry validation tests (every `DocumentType` is well-formed), and end-to-end "ingest a fixture and assert" tests.

### 4.7 Cost guardrails

- **Profiler runs on `claude-haiku-4-5`** (the cheap routing/classification task, already eval-validated for the query classifier), not Sonnet.
- Profiler reads only the **first few pages** of a document (type/department/date live on page 1), not the full text.
- Constraining extraction output to a Pydantic schema can *reduce* output tokens vs. free-form.
- Net new cost ≈ one Haiku call per document, on a corpus ingested only a few dozen times per quarter. The expensive paths (vision parsing of slide decks; query-time synthesis) are unchanged.

## 5. Components / files

**New**
- `src/ingestion/registry.py` — `DocumentType` specs + lookup.
- `src/ingestion/profiler.py` — agentic `DocumentProfile` (Haiku, first-N-pages).
- Per-type schemas — `src/ingestion/schemas/` (or extend `models.py`): `ResolutionMeta`, `ResolutionExtraction`, etc.
- `tests/fixtures/` (golden docs + `expected.json`) and `tests/ingestion/` (hybrid harness).

**Modified**
- `src/ingestion/pipeline.py` — wire profiler + registry + quarantine; bounded worker pool in `ingest_directory`.
- `src/ingestion/metadata.py` — demote filename regex to a hint provider.
- `src/ingestion/chunker.py` — accept chunking hints from the registry.
- `src/ingestion/classifier.py` — use per-type content vocabulary; concurrent classification.
- `src/extraction/sql_extractor.py`, `src/extraction/graph_extractor.py` — schema-driven via the registry.
- `src/storage/sql_store.py`, `src/storage/graph_store.py` — add `resolutions` + `votes` tables; `Resolution`, `Vendor`, `CouncilMember` nodes.
- `src/models.py` — `DocumentProfile`, `DocumentType`, `ChunkMetadata` sourcing from profile; `needs_review` flag.
- `src/config.py` — worker-pool size, profiler model, confidence threshold, profiler page count.

## 6. Decisions locked with the user

| Decision | Choice |
|---|---|
| Autonomy level | Schema-guided agentic (registry of declared types; agent classifies + extracts against it) |
| Testing | Hybrid — exact-match on hard facts + LLM-judge on the fuzzy parts |
| Scope | Framework + registry, migrate quarterly reports, add **Resolutions** as the first new type |
| Re-ingest existing QRs | Yes — through the new content-derived path |
| Unknown documents | Quarantine (vector-only, flagged), never auto-create schema |
| Registry format | Python/Pydantic |
| Speed | Bounded-concurrency worker pool for document-level parallelism |

## 7. Open items (lower stakes — can be decided during implementation)

- Default worker-pool size and confidence threshold (start ~5 workers, tune to API plan).
- Exact `resolutions` / `votes` table columns and graph relationship names (draft in §"Future Document Type: Resolutions" of `harrisburg_knowledge_base_spec.md`).
- Whether per-type schemas live in a new `schemas/` package or in `models.py` (lean: new package once there's more than one type).
