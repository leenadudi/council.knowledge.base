# Resolution Extraction Quality — Design Spec

**Date:** 2026-06-30
**Status:** Draft — pending user review
**Context:** First live ingestion of 7 scanned resolution PDFs (the agentic pipeline + dashboard work) surfaced extraction-quality problems. Classification was perfect (7/7 → `resolution`) and votes extracted consistently, but the structured extraction over many vision-produced page-chunks **over-extracted and cross-contaminated**: Resolution 2's doc spawned a bogus `7-2026` row carrying Res 2's vendor/amount; Resolution 3 (budget roll-forward) extracted nothing; 3 of 7 rows missing amounts; and the graph holds 14 `CouncilMember` nodes for a ~7–9-member council (name variants).

---

## 1. Problem & root cause

These resolution PDFs are scanned, so the detector routes them to the **vision parser** (Claude Sonnet), which chunks **per page** (the keep-together clause logic is bypassed on the vision path). `extract_for_type` then makes a **single open-ended call over all page-chunks** asking the model to "extract all resolutions," which on large/table-heavy docs hallucinates extra resolutions, mislabels numbers, or misses entirely.

Key simplifying fact (confirmed with the user): **each PDF is exactly one resolution.** So the fix is to *anchor* extraction to the single resolution the profiler already identified, rather than re-architecting chunking.

## 2. Goals

1. **Anchored single-resolution extraction:** for one-per-file types, extract exactly ONE primary record keyed to the identifier the profiler found, eliminating hallucinated duplicates and cross-contamination.
2. **Council-member name normalization:** canonicalize names so the graph stops fragmenting one person into multiple nodes.
3. **Validate by re-ingesting the 7 resolutions** and confirming clean data.

## 3. Non-Goals

- **No vision-path chunking change.** Page-chunks are fine for vector retrieval, and anchored extraction reads the full document text regardless of chunking. (Dropped as YAGNI given one-resolution-per-file.)
- **No roster matching for names.** Just string cleaning — we have no authoritative council roster, and composition changes with election cycles.
- No change to the quarterly-report extraction path (`extract_chunks_batched`), which is unaffected and must stay unchanged.

## 4. Design

### 4.1 `anchor_field` on the document type (registry-driven)

Add an optional attribute to `DocumentType`:

```python
anchor_field: Optional[str] = None   # e.g. "resolution_number" — the identifier that uniquely keys the single primary record in a one-record-per-document type
```

The `resolution` registry entry sets `anchor_field="resolution_number"`. Future one-per-file types (ordinance, contract) set theirs. When `anchor_field` is unset, extraction behaves exactly as today (no anchoring) — so quarterly reports and any multi-record type are unaffected.

### 4.2 Anchored extraction in `extract_for_type`

`SQLExtractor.extract_for_type(chunks, doc_type, profile=None)` — new optional `profile` (a `DocumentProfile`).

When `doc_type.anchor_field` is set **and** `profile.identifying_ids` contains that field:

1. **Prompt anchor block** prepended to the extraction instructions:
   > "This document is a SINGLE {doc_type.name}. Its {anchor_field} is **{value}** (department: {profile.department}, period: {profile.period}). Extract exactly ONE primary record for THIS document, plus its vote record. Do NOT invent additional {doc_type.name}s or split it into multiple records."

2. **Deterministic post-extraction guard** (does not rely on the model obeying): after parsing/validating the response, reduce the primary table's rows to **exactly one** — prefer the row whose `{anchor_field}` matches the profiler's value; if none match, take the first; then **force** that row's `{anchor_field}` to the profiler's value. Drop any other primary rows. Votes are retained as-is (a single resolution's vote list). This is what deterministically eliminates the bogus duplicate / contamination.

When `anchor_field` is unset or the id is absent, neither step runs (unchanged behavior).

The primary table is the first entry in `doc_type.sql_targets` (e.g. `resolutions`); votes (`votes`) are the secondary table and are not collapsed.

### 4.3 Thread the profile through the pipeline

`ingest_document` already computes `profile`. Thread it: `_store_chunks(chunks, source_file, doc_type, quarantined, profile)` → `extract_for_type(chunks, doc_type, profile=profile)`. No behavior change for quarantined/quarterly-report paths.

### 4.4 Council-member name normalization

New helper `src/ingestion/names.py`:

```python
def normalize_person_name(name: str) -> str:
    # 1. strip, collapse internal whitespace
    # 2. strip leading honorifics/titles: Council member, Councilmember, Councilman,
    #    Councilwoman, President, Vice President, Mr., Mrs., Ms., Dr.
    # 3. Title-case the remainder
    # returns "" for empty/None-ish input
```

Applied in `_write_typed_data` to each extracted vote's `council_member` **before** storage, so the `votes` SQL rows and the graph `CouncilMember` nodes share one canonical spelling. Members for the graph are then derived from the normalized names (deduped set).

### 4.5 Validation (re-ingest the 7)

Operator step (live DB): re-run ingestion on the 7 resolution PDFs (idempotent clear-then-reinsert removes the existing bad rows). Then confirm:
- exactly **7 distinct resolution_numbers**, **no duplicates** (the bogus `7-2026` is gone),
- **Res 3 has a record** (amount may be null — acceptable for a roll-forward — but number + adopted_date + votes present),
- amounts present where the document states one,
- `CouncilMember` node count drops toward the true ~7–9.

## 5. Error handling

- `extract_for_type` still **never raises** (returns `{}` on any failure); the anchor block and guard are best-effort within that contract.
- The deterministic guard runs only when `anchor_field` + profiler id are present; otherwise it is a no-op.
- A resolution legitimately lacking a single amount yields `amount=null`, not a failure.

## 6. Testing

- **Unit (`tests/extraction/`):** with a fake LLM client, (a) when `profile` carries `resolution_number`, the prompt sent to the client contains that number (anchor block present); (b) given a payload containing TWO resolutions (one bogus, like the real `7-2026` contamination), the guard returns **exactly one** row whose `resolution_number` equals the profiler's value. A control test confirms that with `anchor_field` unset, behavior is unchanged (multi-row passes through).
- **Unit (`tests/ingestion/test_names.py`):** `normalize_person_name` cases — whitespace, title stripping ("Councilman Jones" → "Jones"), case folding ("SMITH" → "Smith"), empty input → "".
- **Operator/integration:** the §4.5 re-ingest validation (needs live Supabase + Neo4j + API keys).

## 7. Files

**New**
- `src/ingestion/names.py` (+ `tests/ingestion/test_names.py`)
- `tests/extraction/test_anchored_extraction.py`

**Modified**
- `src/models.py` — add `anchor_field: Optional[str] = None` to `DocumentType`.
- `src/ingestion/registry.py` — resolution entry sets `anchor_field="resolution_number"`.
- `src/extraction/sql_extractor.py` — `profile` param, anchor prompt block, deterministic single-record guard.
- `src/ingestion/pipeline.py` — thread `profile` into `_store_chunks`/`extract_for_type`; normalize vote member names in `_write_typed_data`.

## 8. Decisions locked with the user

| Decision | Choice |
|---|---|
| Doc structure | One resolution per file |
| Core fix | Anchored single-record extraction via registry `anchor_field` + deterministic guard |
| Names | String-clean normalization only (no roster matching) |
| Vision chunking | Not changed (YAGNI) |
| Validation | Re-ingest the 7 resolutions; check distinct/clean data |

## 9. Open items

- Exact honorific list for `normalize_person_name` (start with the set in §4.4; easy to extend).
- Whether to later extend `anchor_field` anchoring to ordinances/contracts when those types are added (out of scope now; the mechanism already supports it).
