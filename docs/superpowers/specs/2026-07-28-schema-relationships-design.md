# Schema Relationships + Departments Dimension — Design

**Date:** 2026-07-28
**Status:** Draft, pending user review
**Scope:** One additive, non-destructive SQL migration + one small ingest helper. No table rebuilds, no data loss, no LLM spend.

---

## 1. Problem

The Supabase schema has ~20 tables and **exactly one** declared foreign key
(`evaluation_results.question_id → evaluation_suite.id`). Every other table renders
as an isolated box in Supabase's schema diagram. Two distinct problems underlie this:

**(a) Relationships exist in the data but are never declared.** Provenance columns
copied across the fact tables already point at real tables — Postgres just can't see it:

| Column | Actually references | Target is |
| --- | --- | --- |
| `source_chunk_id` | `document_chunks.chunk_id` | PRIMARY KEY (real pgvector table) |
| `source_file` | `documents.source_file` | already UNIQUE |
| `chunk_performance.chunk_id` | `document_chunks.chunk_id` | PRIMARY KEY |
| `votes.resolution_number` | `resolutions.resolution_number` | not unique (see §3.4) |
| `meeting_actions` (by date) | `meetings` | no stable key (see §3.4) |

**(b) `department` is a free-text string, duplicated across 9 tables, with no
dimension table.** Departments exist as canonical entities in the *graph* store
(`Department` nodes) but not in SQL. Extraction produces drifting variants — observed
in the live document set:

- `Bureau of Police` vs `Buraeu of Police` (typo)
- `Bureau of Communications` vs `Bureau of Coummunicaitons` (typo)
- `Department of Finance` vs `Departnment of Finance` (typo)
- `Park Maintenance` vs `Parks Maintenance`
- `Office of Equity & Compliance` vs `Office of Equity and Compliance`
- `Department of Facilities & Special Projects` vs `... and Special Projects`

Because there is no canonical key, you cannot reliably join or aggregate one
department across budget + goals + grants + votes — which is exactly the
**cross-table synthesis** the product is meant to win on.

### Tables carrying a `department` string (9)
`expenditures`, `metrics`, `grants`, `vacancies`, `documents`, `resolutions`,
`appropriations`, `goals`, `document_chunks`.

(`votes`, `meetings`, `meeting_actions`, `legislation` do **not** carry `department`.)

---

## 2. Goals & non-goals

**Goals**
- Declare the foreign keys that already exist in the data, so the schema diagram
  reflects the true relational model and Postgres enforces provenance integrity.
- Introduce a real `departments` dimension every department-bearing table references.
- Do it **additively**: keep every existing column, break no insert or query path.
- **Deterministic, zero LLM spend** — department canonicalization is a static alias
  map derived from logic the app already uses, not an AI resolver.
- Idempotent, re-runnable migration consistent with the existing `sql/migrate_*` style.

**Non-goals (explicitly out of scope for this migration)**
- Dropping the raw `department` string columns (kept as an audit trail — user decision).
- A shared time dimension (`quarter`/`year` stays denormalized for now).
- Unifying `resolutions`/`legislation`/`appropriations` under shared keys.
- RLS / auth / access-control changes.
- Re-ingesting or re-extracting any document.

---

## 3. Design

### 3.1 `departments` + `department_aliases`

```sql
CREATE TABLE IF NOT EXISTS departments (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,   -- "Bureau of Fire"
    canonical_key  VARCHAR(120) NOT NULL UNIQUE,    -- dashboard _dept_key, e.g. "fire", "parks & recreation"
    dept_type      VARCHAR(30),                     -- bureau | department | office | council | other
    parent_id      INTEGER REFERENCES departments(id),  -- optional rollup, populated later
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS department_aliases (
    id             SERIAL PRIMARY KEY,
    department_id  INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    alias          VARCHAR(150) NOT NULL UNIQUE     -- raw string as it appears in source data
);
CREATE INDEX IF NOT EXISTS idx_dept_aliases_alias ON department_aliases(LOWER(alias));
```

**Why an aliases table rather than a single canonical column:** extraction emits
variants and typos indefinitely. The alias table is the deterministic lookup that
resolves any raw string → one `departments.id`, and is the single mechanism used by
*both* the one-time backfill and ongoing ingestion (§3.7). Adding a newly-seen
variant later is a one-row insert, not a code change.

`canonical_key` deliberately matches the dashboard's existing canonical key
(`_dept_key_base` + `_DEPT_ALIASES`, `aggregator.py:265-291`) so the SQL dimension and
the dashboard never disagree about what "one department" is.

### 3.2 `department_id` on the 9 department-bearing tables

For each of `expenditures, metrics, grants, vacancies, documents, resolutions,
appropriations, goals, document_chunks`:

```sql
ALTER TABLE <t> ADD COLUMN IF NOT EXISTS department_id INTEGER REFERENCES departments(id);
CREATE INDEX IF NOT EXISTS idx_<t>_department_id ON <t>(department_id);
```

- The existing `department` VARCHAR is **kept** (raw extracted value = audit trail).
- `department_id` is **nullable**, backfilled via the alias map (§3.6).
- Unmapped strings stay `NULL` and are logged to `review_flags` — you add one alias
  row and re-run the backfill; no data is ever dropped.

### 3.3 Provenance foreign keys

Pipeline insert order is **documents → chunks → facts**
(`pipeline.py:204` record_document, `:298` upsert_chunks, `:320+` fact inserts), so
these constraints never fire against a missing parent on new writes.

Applied per table (only where the column exists):

| Constraint | Tables |
| --- | --- |
| `source_file → documents(source_file)` ON DELETE SET NULL | expenditures, metrics, grants, resolutions, votes, meetings, meeting_actions, legislation, appropriations, goals, document_chunks |
| `source_chunk_id → document_chunks(chunk_id)` ON DELETE SET NULL | expenditures, metrics, grants, vacancies, resolutions, votes, meetings, meeting_actions, legislation, appropriations, goals |
| `chunk_id → document_chunks(chunk_id)` ON DELETE CASCADE | chunk_performance |

```sql
ALTER TABLE <t> ADD CONSTRAINT fk_<t>_source_file
    FOREIGN KEY (source_file) REFERENCES documents(source_file)
    ON DELETE SET NULL NOT VALID;

ALTER TABLE <t> ADD CONSTRAINT fk_<t>_source_chunk
    FOREIGN KEY (source_chunk_id) REFERENCES document_chunks(chunk_id)
    ON DELETE SET NULL NOT VALID;
```

- **`NOT VALID`**: the FK appears in the diagram and is enforced on all new/updated
  rows *immediately*, but Postgres skips scanning pre-existing rows — so a legacy
  orphan (e.g. a fact whose chunk was replaced by re-ingest) cannot fail the
  migration. A later `VALIDATE CONSTRAINT` pass (§4, optional) confirms cleanliness
  after an orphan sweep.
- **`ON DELETE SET NULL`**: re-ingesting or deleting a chunk/document nulls the link
  instead of cascading a delete into hard-won fact rows.
- Note: `vacancies` has no `source_file` column today; only the chunk FK applies.
  Optionally add `source_file` to `vacancies` for parity (low priority — flagged, not
  assumed).

### 3.4 `votes → resolutions` and `meeting_actions → meetings` (best-effort)

The natural keys are dirty: `insert_resolution_rows` has no de-dup
(`sql_store.py:244`), so `resolution_number` can repeat; `meetings` has only
`meeting_date`. Rather than force a risky `UNIQUE` onto dirty data, add surrogate FK
columns and backfill by matching the natural key:

```sql
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS resolution_id INTEGER REFERENCES resolutions(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS meeting_id    INTEGER REFERENCES meetings(id);
```

Backfill: for each `votes` row, match `resolution_number` to the **earliest**
`resolutions.id` with that number (`MIN(id)`); for each `meeting_actions` row, match
`meeting_date` to the earliest `meetings.id` on that date. Rows where the natural key
is duplicated (ambiguous) or absent (unmatched) are logged to `review_flags` with the
count, and left `NULL`. This gets the edges into the diagram now, with imperfections
made visible instead of silently guessed.

### 3.5 Seed: canonical departments + aliases (grounded, pending your approval)

Derived deterministically by applying the dashboard's own canonicalization
(`_dept_key_base`: strip `bureau of|department of|office of|the ` prefix, `" and " →
" & "`, collapse punctuation; then `_DEPT_ALIASES`) to the distinct department
strings observed in the document set. First-cut canonical set:

| canonical_name | canonical_key | dept_type | aliases to fold in |
| --- | --- | --- | --- |
| Bureau of Police | police | bureau | Buraeu of Police *(typo)* |
| Bureau of Fire | fire | bureau | |
| Bureau of Codes | codes | bureau | Codes Health Department |
| Bureau of Communications | communications | bureau | Bureau of Coummunicaitons *(typo)* |
| Bureau of Human Resources | human resources | bureau | |
| Bureau of Information Technology | information technology | bureau | |
| Bureau of Licensing, Taxation and Central Support | licensing taxation & central support | bureau | |
| Bureau of Parks & Recreation | parks & recreation | bureau | Park Maintenance, Parks Maintenance |
| Bureau of Planning | planning | bureau | Planning Bureau |
| Department of Public Works | public works | department | |
| Department of Budget & Finance | budget & finance | department | Department of Finance, Departnment of Finance *(typo)* |
| Department of Building & Housing Development | building & housing development | department | Department of Economic Development & Building and Housing |
| Department of Facilities & Special Projects | facilities & special projects | department | Department of Facilities and Special Projects |
| Office of Equity & Compliance | equity & compliance | office | Office of Equity and Compliance |
| Office of Human Relations Contract Compliance and Standards | human relations contract compliance & standards | office | |
| City Controller | city controller | office | |
| Health Office | health office | office | |
| Grants Department | grants | department | |
| Tax Enforcement | tax enforcement | other | |
| City Council | city council | council | Harrisburg City Council, City of Harrisburg City Council |

**Flagged judgment calls that need your eye before we run anything:**

1. **`America Rescue Plan Act`** appears as a "department" but is a *funding source*
   (ARPA), not a department. Recommend: **exclude** from `departments`; leave those
   rows' `department_id` NULL (or later attach to whichever department administers the
   grant). Your call.
2. **`Department of Building & Housing and Business Development`** — same as
   `...Building & Housing Development`, or a distinct org (adds "Business
   Development")? Recommend folding into one; confirm.
3. **`Grants Department`** — real standalone department, or an alias of another org?
   Kept standalone for now.
4. **`Health Office` vs `Bureau of Codes` (alias "Codes Health Department")** — the
   existing dashboard alias merges "codes health department" → codes. Confirm Health
   Office is genuinely separate from Codes.

No canonical/alias rows are written until you approve this table.

### 3.6 Backfill strategy (deterministic, no LLM)

Executed inside the migration, in order:

1. Insert the approved `departments` rows and `department_aliases` rows.
2. Insert each canonical name as its own alias (so exact matches resolve too).
3. For each department-bearing table:
   `UPDATE <t> SET department_id = da.department_id
    FROM department_aliases da
    WHERE LOWER(TRIM(<t>.department)) = LOWER(da.alias) AND <t>.department_id IS NULL;`
4. Log any remaining distinct unmatched `department` values (per table) into
   `review_flags(stage='validate', reason='unmapped_department', detail=<value>)`.

Because matching is on a static alias table, the same input always yields the same
result — re-running is safe and converges as you add aliases.

### 3.7 Ingest code change (one helper)

Add to `sql_store.py`:

```python
def resolve_department_id(self, name: str | None) -> int | None:
    """Deterministic alias-map lookup. No LLM. Returns None (and flags) if unmapped."""
```

Each `insert_*_rows` that carries `department` sets `department_id =
resolve_department_id(department)` alongside the existing string write. An unmapped
name inserts a `review_flags` row (dedup'd) so it surfaces for a one-line alias add.
This is the only Python change; the raw `department` string continues to be written
exactly as today, so nothing downstream breaks.

---

## 4. Migration mechanics

- **One file:** `sql/migrate_2026_07_28_schema_relationships.sql`, matching the
  existing idempotent style (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`,
  `ADD CONSTRAINT` guarded so re-runs don't error).
- **Order:** create `departments`/`department_aliases` → seed (approved list) → add
  `department_id` columns + indexes → add surrogate `resolution_id`/`meeting_id`
  columns → backfill all links → add provenance FKs `NOT VALID` → flag unmatched.
- **Validation (optional, follow-up):** after an orphan sweep, run
  `ALTER TABLE <t> VALIDATE CONSTRAINT <fk>;` to promote each `NOT VALID` FK to fully
  verified. Safe to defer; enforcement on new rows is already active.
- **Rollback:** every step is reversible — `DROP CONSTRAINT`, `DROP COLUMN
  department_id / resolution_id / meeting_id`, `DROP TABLE department_aliases,
  departments`. Raw `department` strings are untouched, so rollback is lossless.
- `sql/schema.sql` is updated so a fresh bootstrap creates the same end state.

---

## 5. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Legacy orphan rows (chunk/doc deleted by re-ingest) would fail a normal FK | `NOT VALID` constraints skip existing rows; enforced on new writes only |
| Duplicate `resolution_number` makes votes→resolutions ambiguous | Surrogate `resolution_id`, earliest-match, ambiguities logged not guessed |
| Insert path writes a fact before its document row | Verified order is documents→chunks→facts; not possible in current pipeline |
| Dashboard and SQL dimension disagree on "one department" | `canonical_key` seeded from the dashboard's own `_dept_key`; single source of truth |
| A new department variant appears after migration | `resolve_department_id` flags it to `review_flags`; add one alias row |
| Unmapped `department` silently lost | Never dropped — string kept, `department_id` NULL, flagged for review |

---

## 6. Verification

- **Diagram:** Supabase schema view shows `documents` and `document_chunks` as hubs,
  `departments` as a spine across the 9 tables, and `votes`/`meeting_actions` linked.
- **Backfill coverage:** report `count(department_id NOT NULL) / count(*)` per table;
  investigate any table with many NULLs against the `review_flags` list.
- **Ingest smoke test:** dry-run the ingest path on one already-ingested document
  (no new LLM calls needed if re-using cached extraction) and confirm rows write with
  `department_id` populated and no FK violations.
- **Dashboard unchanged:** `_build_departments` still returns the same groupings
  (slugs were seeded from its own logic).

---

## 7. Open items requiring your approval

1. The canonical department table in §3.5, especially the four flagged judgment calls.
2. Whether to add `source_file` to `vacancies` for provenance parity (optional).
3. Whether to include the optional `llm_usage.query_id → query_logs.query_id` link
   (clean, but `query_id` is null/foreign for ingest-time calls — left out of core).
