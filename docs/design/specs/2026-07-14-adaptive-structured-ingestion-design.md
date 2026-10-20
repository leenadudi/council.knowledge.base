# Adaptive Structured Ingestion — Triage, Reconcile, Data-Driven Types

**Status:** Design (approved shape 2026-07-14; pending spec review)

**Goal:** When a document arrives whose valuable structured data has no home in the
current schema, an agent detects it, reconciles it against the *existing* schema, and
proposes where the data should go — mapping into existing tables first and proposing a
new table only for genuine gaps. A human approves; approval makes the type real (data,
not code) so it extracts and becomes queryable with no deploy.

**Architecture:** A triage step on the ingest path (mirrors the read-side query
classifier) → a review queue → an approval action that reconciles-or-creates schema →
a data-driven type registry that the extractor and query classifier both read live.

**Tech stack:** Python, Pydantic (runtime model compilation), Postgres/Supabase
(`document_type_registry` + a new `type_proposals` table), Anthropic Haiku (triage),
the existing schema-driven extractor and Flask dashboard.

## Global Constraints

- **Accuracy is the #1 pillar.** No structured row and no schema change reaches the DB
  without passing the existing per-row validation + confidence filter + `_clip`, and —
  for schema changes — an explicit human approval. The agent *proposes*; it never
  mutates schema autonomously.
- **Limited funds.** Triage runs Haiku, and ONLY on documents the profiler returns as
  `unclassified` / low-confidence — never on every doc. No new per-doc LLM cost on the
  already-classified path.
- **Don't break the read side.** The query classifier writes SQL against known tables;
  any new table must be registered in the live schema the classifier reads, or its data
  is unqueryable. Data-driven registry is what keeps write and read in sync.
- Reuse existing infrastructure wherever possible (see below); avoid parallel mechanisms.

---

## Problem & Current State

The ingest path can only route documents whose `document_type` is one of 5 hardcoded
types in [`src/ingestion/registry.py`](../../../src/ingestion/registry.py)
(`quarterly_report`, `resolution`, `minutes`, `legislation`, `budget`). The profiler
([`src/ingestion/profiler.py`](../../../src/ingestion/profiler.py)) classifies each doc
against that fixed menu and returns `"unclassified"` for anything else. Unclassified
docs hit the quarantine gate ([`pipeline.py` `_is_quarantined`](../../../src/ingestion/pipeline.py))
and are stored **vector-only** — searchable, but nothing in the structured tables.

So a document like the *Boards, Commissions & Authorities* booklet — dense with rosters,
appointment→resolution references, term expirations, and vacancies — captures **zero**
structured data, and onboarding it today means hand-building a Pydantic schema + registry
entry + query-classifier prompt edit + deploy. That manual loop is the problem.

**Hooks that already exist and are reused here:**
- The profiler already emits a `proposed_type` field for "a real type not in the list" —
  currently unconsumed. This is the triage trigger.
- `document_type_registry` **table exists** in [`sql/schema.sql`](../../../sql/schema.sql)
  (seeded with `quarterly_report`) but the runtime registry is Python. Closing that gap
  is the "data-driven" refactor.
- `review_flags` establishes the human-review-queue pattern.
- The extractor is already schema-driven: `quarterly_prompt(texts, schema_cls)` builds
  the prompt from `schema_cls.model_json_schema()`. It needs the schema from the DB
  rather than a Python class.

---

## Design Overview

```
INGEST
  parse → profile (Haiku)
    │
    ├─ known type (confident)      → existing routing (unchanged)
    └─ unclassified / low-conf     → TRIAGE AGENT (Haiku, cheap)
          "Is there structured, record-like data worth storing?
           If yes: identify each record-type, extract sample rows,
           and RECONCILE against the live schema."
             │
             ├─ NO structured data     → vector-only (correct; e.g. a narrative memo)
             └─ YES → for each record-type the agent proposes ONE of:
                        • FIT: maps into an existing table  (+ column mapping + confidence)
                        • NEW: needs a new table            (+ proposed columns/types)
                      → write a PROPOSAL row (status=pending) + surface in dashboard

REVIEW (human, in dashboard)
  sees per record-type: target (existing table + mapping | proposed new table),
  the drafted schema, and 5 sample rows with source_text.
    → Approve  → APPLY
    → Reject   → doc stays vector-only; proposal archived

APPLY (guarded)
  • FIT into existing table   → register a mapping; extract + insert (no DDL)
  • NEW table                 → guarded CREATE TABLE from approved schema,
                                 register the type in document_type_registry (DB),
                                 extract + insert
  → future docs like it are now classified + extracted automatically, and the
    query classifier (reading the live registry) can query them.
```

---

## Components

### 1. Triage agent
- **Where:** new `src/ingestion/triage.py`, invoked from `pipeline.py` when
  `_is_quarantined` is true *because the type is unknown* (not because parse/garble
  failed — those keep their existing quarantine path).
- **Model:** Haiku (`cfg.profile_model` tier), one call per unclassified doc.
- **Input:** the assembled document text (capped/sampled for cost on very large docs) +
  the **live schema summary** (every table and its columns, from the registry).
- **Output (structured, validated):** a `TriageResult`:
  ```
  has_structured_data: bool
  record_types: [
    { name, description,
      target: "existing" | "new",
      existing_table: str|null,           # when target=existing
      column_mapping: {doc_field: table_column}|null,
      proposed_columns: [{name, type, description}]|null,   # when target=new
      match_confidence: float,
      sample_rows: [ {..., source_text} ]  # up to 5, for human judgment
    }, ...
  ]
  proposed_type_name: str                 # e.g. "boards_commissions"
  ```
- **Conservatism rule (accuracy):** the agent only chooses `existing` when the record is
  the *same kind* as that table's rows (not merely column-similar). When unsure it must
  choose `new` or flag low `match_confidence`; the human decides. This prevents corrupting
  an existing table's semantics.

### 2. Proposal queue (data model)
New table (cleaner than overloading `review_flags`):
```sql
CREATE TABLE IF NOT EXISTS type_proposals (
    id            SERIAL PRIMARY KEY,
    source_file   VARCHAR(255) NOT NULL,
    proposed_type VARCHAR(100),
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    payload       JSONB NOT NULL,        -- the full TriageResult (record_types, mappings, samples)
    created_at    TIMESTAMP DEFAULT NOW(),
    reviewed_at   TIMESTAMP,
    reviewer_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_type_proposals_status ON type_proposals(status);
```

### 3. Dashboard review panel
- A "Proposed data types" panel in the Flask dashboard listing `pending` proposals.
- Per proposal, per record-type, shows: the target (existing table + the exact column
  mapping, OR the proposed new table + columns/types), and the 5 sample rows with their
  `source_text` so the reviewer can judge extraction quality before anything writes.
- Actions: **Approve** / **Reject** (+ optional note). Approve triggers §4.
- Note: this rides on the existing (unauthenticated) dashboard — see Risks; approval is a
  schema-mutating action, so if/when auth is added this route must be gated.

### 4. Approval → Apply (guarded)
- **FIT (existing table):** no DDL. Persist the approved `column_mapping` (as part of the
  registered type), then run extraction and insert mapped rows through the **existing**
  insert path for that table (inherits `_clip`, per-row validation, atomicity).
- **NEW table:** a controlled `CREATE TABLE` generated from the approved schema:
  - column names validated (`^[a-z][a-z0-9_]*$`, not a reserved word);
  - types from a **whitelist** only: `TEXT`, `VARCHAR(n)`, `INTEGER`, `DECIMAL(15,2)`,
    `DATE`, `BOOLEAN`;
  - standard columns always appended to match sibling tables:
    `id SERIAL PK, source_chunk_id UUID, source_file VARCHAR(255), ingested_at TIMESTAMP`;
  - `CREATE TABLE IF NOT EXISTS` + a `source_file` index (addresses the review finding
    that structured tables lack one);
  - **never** `DROP`/`ALTER` an existing table.
- Register the type by inserting/updating a `document_type_registry` row (see §5).

### 5. Data-driven registry
- `document_type_registry` becomes the **source of truth**. Columns used:
  `type_name, description, extraction_templates (JSON schema), sql_tables,
  content_type_rules, active`. Add a `column_mappings JSONB` for FIT mappings.
- `src/ingestion/registry.py` loads types from the DB at startup (and on demand),
  compiling each stored JSON schema into a Pydantic model at runtime via
  `pydantic.create_model(...)`. The 5 current types are **seeded** into the table
  (migration) so behavior is unchanged; nothing about their extraction changes.
- The schema-driven extractor (`extract_for_type` / the `quarterly_prompt` builder) reads
  the compiled schema — no code change to the extraction mechanism itself.

### 6. Schema-aware query classifier
- Today `_CLASSIFY_PROMPT` in [`src/query/classifier.py`](../../../src/query/classifier.py)
  hardcodes the table list. Refactor so the "Tables" section is **generated from the live
  registry** (table names + columns + per-column notes). Approving a new type then makes
  it queryable with no prompt edit.
- The existing regression guards (quarter is `'Q1'`, grants has no quarter/year, literal
  Cypher) are preserved as static rules appended to the generated schema section.

---

## Accuracy Guardrails (the critical section)

1. **Human gate on every schema decision** — mappings into existing tables AND new
   tables both require explicit approval, with sample rows shown first.
2. **Conservative matching** — the agent must justify an `existing` match by record-kind,
   not column shape; low confidence → surfaced as `new`/uncertain, never auto-fit.
3. **No autonomous DDL** — `CREATE TABLE` only from an approved schema, whitelisted
   types, no destructive ops, never touching existing tables.
4. **Extraction inherits all protections** — new/mapped types run through per-row
   validation, the confidence filter, `_clip`, and atomic writes already in place.
5. **Reversibility** — a rejected proposal leaves the doc vector-only (no change). A newly
   created table is additive; misfires are dropped tables, not corrupted existing data.
6. **Cost ceiling** — triage is Haiku-only and gated to unclassified docs; log every
   triage call's cost via the existing `TrackedAnthropic`.

---

## Build Milestones (end-to-end, but shipped incrementally)

Each milestone is independently testable and leaves the system working.

- **M1 — Triage + proposal queue (read-only proposals).** `triage.py`, `TriageResult`
  schema, `type_proposals` table, wire into the unclassified branch, dashboard panel that
  *displays* proposals. No apply yet. Delivers detection + drafted schema + sample rows.
- **M2 — Data-driven registry.** Move the registry to read from `document_type_registry`
  (seed the 5 existing types), runtime JSON→Pydantic compilation, extractor reads DB
  schema. No behavior change for existing types (regression-tested).
- **M3 — Approve → Apply.** Guarded `CREATE TABLE`, FIT-mapping application, registry
  row insert, extraction + insert for the approved type. Approvals now produce real,
  extracting types.
- **M4 — Schema-aware query classifier.** Generate the classifier's table section from
  the live registry so approved types are immediately queryable + dashboardable.

## Testing Strategy

- Unit: `TriageResult` validation; conservative-match logic (fixture docs → expected
  fit/new decisions); DDL generator (whitelist enforcement, rejects bad column names/types,
  never emits DROP/ALTER); JSON→Pydantic compilation round-trip; registry-from-DB parity
  with the current Python registry (the 5 seeded types compile to equivalent schemas).
- Integration (DB-marked): approve a proposal → table created → rows inserted → row
  re-ingest is idempotent (`source_file` delete works on the new table).
- Regression: existing extraction/query tests must pass unchanged after M2's registry
  swap (this is the riskiest refactor).
- The boards booklet is the end-to-end acceptance fixture.

## Risks & Open Questions

- **M2 registry refactor is the highest-risk change** — the whole pipeline reads the
  registry. Mitigate with strict parity tests before switching the source of truth.
- **Dashboard is unauthenticated** (deferred per owner). Approval is schema-mutating; note
  for the auth work whenever it happens.
- **Partial-fit documents** (some record-types FIT, some NEW) must be handled in one
  proposal — the data model supports it (`record_types` list); the UI must present it clearly.
- **Query classifier prompt growth** — many types could bloat the prompt; may later need
  retrieval of only relevant tables. Out of scope now.
- **Open:** should FIT-mappings also derive graph edges (e.g. appointment→resolution),
  or SQL only for v1? Leaning SQL-only for v1.

## Out of Scope (v1)

- Auto-promotion / merging of similar proposed types over time.
- Editing an existing type's schema via the agent (add-only; schema evolution stays manual).
- Backfilling already-ingested vector-only docs (a later re-triage pass could).
- Authentication on the approval route (tracked separately).
