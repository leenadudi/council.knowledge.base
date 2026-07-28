# Schema Relationships + Departments Dimension — Design

**Date:** 2026-07-28
**Status:** Draft, pending user review
**Scope:** One additive, non-destructive SQL migration + one small ingest change. No
table rebuilds, no data loss, no LLM spend. **City-agnostic:** nothing about any
specific city's departments is hardcoded — the dimension populates itself from
ingested data.

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
dimension table.** Extraction produces drifting variants — observed in the live data:
`Bureau of Police`/`Buraeu of Police` (typo), `Bureau of Communications`/`Bureau of
Coummunicaitons` (typo), `Office of Equity & Compliance`/`Office of Equity and
Compliance`. With no canonical key you cannot reliably join or aggregate one
department across budget + goals + grants + votes — the **cross-table synthesis** the
product is meant to win on.

**(c) Multi-city constraint.** This schema is the flagship for Harrisburg but is
intended to be **redeployed per city** (each city = its own database, same schema —
see §2). Every city has different departments. Therefore **no department names,
aliases, or merge rules may be hardcoded** into the schema or migration. The
departments dimension must discover itself from whatever data a given city ingests.

### Tables carrying a `department` string (9)
`expenditures`, `metrics`, `grants`, `vacancies`, `documents`, `resolutions`,
`appropriations`, `goals`, `document_chunks`.
(`votes`, `meetings`, `meeting_actions`, `legislation` do **not** carry `department`.)

---

## 2. Goals & non-goals

**Goals**
- Declare the foreign keys that already exist in the data, so the schema diagram
  reflects the true relational model and Postgres enforces provenance integrity.
- Add a `departments` dimension that **populates itself from ingested data** via
  generic, city-agnostic normalization — every department-bearing table references it.
- Do it **additively**: keep every existing column, break no insert or query path.
- **Deterministic, zero LLM spend** — canonicalization is string normalization, not an
  AI resolver.
- Idempotent, re-runnable migration consistent with the existing `sql/migrate_*` style.

**Deployment model:** one database per city, identical schema. No `city_id`/tenant
column — tenancy is the deployment boundary. (Does not preclude a future shared-tenant
migration; simply out of scope here.)

**Non-goals**
- **Any hardcoded or seeded department list, alias, or city-specific merge rule.** The
  migration ships with the `departments`/`department_aliases` tables **empty**; they
  fill from data.
- Multi-tenant `city_id` columns (separate DB per city instead).
- Dropping the raw `department` string columns (kept as audit trail — user decision).
- A shared time dimension; unifying resolutions/legislation/appropriations; RLS/auth;
  re-ingesting or re-extracting any document.

---

## 3. Design

### 3.1 `departments` + `department_aliases` (created empty, self-populating)

```sql
CREATE TABLE IF NOT EXISTS departments (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,   -- the string as first seen in data
    canonical_key  VARCHAR(120) NOT NULL UNIQUE,    -- generic normalized key (see §3.5)
    dept_type      VARCHAR(30),                     -- inferred from prefix: bureau|department|office|council|other
    parent_id      INTEGER REFERENCES departments(id),  -- optional rollup, set by clerk later
    active         BOOLEAN NOT NULL DEFAULT TRUE,   -- set FALSE when merged into another (see §3.8)
    created_at     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS department_aliases (
    id             SERIAL PRIMARY KEY,
    department_id  INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    alias          VARCHAR(150) NOT NULL UNIQUE     -- every raw variant string seen in data
);
CREATE INDEX IF NOT EXISTS idx_dept_aliases_alias ON department_aliases(LOWER(alias));
```

**No seed rows.** Both tables are empty after the migration and fill from ingestion
(§3.5) and the backfill of pre-existing rows (§3.6). `canonical_key` is UNIQUE per
database — which, under one-DB-per-city, means unique per city. `canonical_name` is
just the first-seen spelling; a clerk can rename it without affecting links.

### 3.2 `department_id` on the 9 department-bearing tables

For each of `expenditures, metrics, grants, vacancies, documents, resolutions,
appropriations, goals, document_chunks`:

```sql
ALTER TABLE <t> ADD COLUMN IF NOT EXISTS department_id INTEGER REFERENCES departments(id);
CREATE INDEX IF NOT EXISTS idx_<t>_department_id ON <t>(department_id);
```

- The existing `department` VARCHAR is **kept** (raw extracted value = audit trail).
- `department_id` is **nullable**, populated by discovery (§3.5–3.6).
- A string that can't resolve stays `NULL` and is logged to `review_flags`; no data is
  ever dropped.

### 3.3 Provenance foreign keys

Pipeline insert order is **documents → chunks → facts** (`pipeline.py:204` / `:298` /
`:320+`), so these never fire against a missing parent on new writes.

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
  rows immediately, but Postgres skips scanning legacy rows — so a pre-existing orphan
  (fact whose chunk was replaced by re-ingest) can't fail the migration. A later
  `VALIDATE CONSTRAINT` pass confirms cleanliness after an orphan sweep.
- **`ON DELETE SET NULL`**: re-ingesting/deleting a chunk or document nulls the link
  instead of cascading a delete into hard-won fact rows.
- `vacancies` has no `source_file` column; only the chunk FK applies. Optionally add
  `source_file` to `vacancies` for parity (flagged, not assumed).

### 3.4 `votes → resolutions` and `meeting_actions → meetings` (best-effort)

Natural keys are dirty: `insert_resolution_rows` has no de-dup (`sql_store.py:244`), so
`resolution_number` can repeat; `meetings` has only `meeting_date`. Add surrogate FK
columns and backfill by matching the natural key:

```sql
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS resolution_id INTEGER REFERENCES resolutions(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS meeting_id    INTEGER REFERENCES meetings(id);
```

Backfill matches each `votes.resolution_number` to the **earliest** `resolutions.id`
with that number (`MIN(id)`), and each `meeting_actions.meeting_date` to the earliest
`meetings.id` on that date. Ambiguous (duplicated key) or unmatched rows are logged to
`review_flags` and left `NULL` — imperfections made visible, not silently guessed.

### 3.5 Discovery-based population (replaces any seed)

This is the core of the city-agnostic design. A single deterministic resolver turns a
raw department string into a `department_id`, creating the department the first time
it's seen.

**Generic normalization → `canonical_key`** (city-agnostic; reuse the existing
`_dept_key_base`, `aggregator.py:276`, so SQL and dashboard agree):
1. lowercase, trim, `" and " → " & "`;
2. strip a leading generic municipal prefix — `bureau of `, `department of `,
   `office of `, `the `;
3. collapse remaining punctuation to single spaces.

These rules encode US municipal-naming *shape*, not any city's specific departments —
so `Office of Equity & Compliance` and `Office of Equity and Compliance` collapse to
the same key in *any* city, with nothing hardcoded.

**Resolver (`resolve_department_id`)**, concurrency-safe:
1. Exact-match the raw string against `department_aliases.alias` → return its
   `department_id`.
2. Else compute `canonical_key`; match against `departments.canonical_key` → return id
   and record the raw string as a new alias.
3. Else **auto-create**: `INSERT INTO departments (canonical_name, canonical_key,
   dept_type) VALUES (<raw>, <key>, <inferred from prefix>) ON CONFLICT (canonical_key)
   DO NOTHING RETURNING id` (re-select on conflict to stay race-safe), then insert the
   raw string as an alias, and return the id.

**Near-duplicate guard (deterministic, optional, city-agnostic).** On auto-create,
compare the new `canonical_key` against existing keys with `pg_trgm` similarity; if
above a threshold (e.g. `similarity > 0.6`), also write a `review_flags` row
(`reason='possible_duplicate_department'`) so a clerk can confirm whether e.g.
`buraeu of police` should merge into `bureau of police`. This catches the typo
fragmentation that pure normalization can't — without any hardcoded name list.

### 3.6 Backfill of existing rows = discovery over existing strings

Inside the migration (or a one-shot script it calls), run the §3.5 resolver over the
**distinct `department` strings already present** in the 9 tables, then:

```sql
UPDATE <t> SET department_id = da.department_id
FROM department_aliases da
WHERE LOWER(TRIM(<t>.department)) = LOWER(da.alias) AND <t>.department_id IS NULL;
```

So even Harrisburg's departments are derived from Harrisburg's own data — identical to
how a brand-new city would populate. Remaining unmatched strings are logged to
`review_flags`.

**Optional, clearly-separated Harrisburg convenience (NOT part of the generic
migration):** the dashboard already curates known-good Harrisburg merges in
`_DEPT_ALIASES` (`aggregator.py:265`), e.g. `codes health department → codes`. Since
tenancy is per-DB, these can be replayed into *Harrisburg's* `department_aliases` as a
separate, opt-in data file so we don't lose that curation. It lives beside the
migration, is labeled Harrisburg-only, and ships nothing city-specific in the schema.
Skippable.

### 3.7 Ingest code change (one helper)

Add `resolve_department_id(name) -> int | None` to `sql_store.py` implementing §3.5
(shared normalization with the dashboard). Each `insert_*_rows` that carries
`department` sets `department_id = resolve_department_id(department)` beside the
existing string write. The raw string is still written exactly as today, so nothing
downstream breaks; departments simply accrue as documents are ingested.

### 3.8 Clerk merge operation (how typo-variants get reconciled)

Because population is automatic and city-agnostic, reconciling variants that
normalization can't catch is a **data action, not code**. A `merge_departments(dup_id,
keep_id)` operation:
1. repoint `department_aliases.department_id` from `dup_id` → `keep_id`;
2. `UPDATE <t> SET department_id = keep_id WHERE department_id = dup_id` across the 9
   tables;
3. set `departments.active = FALSE` on `dup_id` (kept for audit, hidden from pickers).

This is the mechanism that folds `Buraeu of Police` into `Bureau of Police` per city.
The SQL/function is in scope; a dashboard UI button for it is a follow-up.

---

## 4. Migration mechanics

- **One file:** `sql/migrate_2026_07_28_schema_relationships.sql`, idempotent
  (`IF NOT EXISTS`, guarded `ADD CONSTRAINT`, `ON CONFLICT DO NOTHING`).
- **Order:** create `departments`/`department_aliases` (empty) + `pg_trgm` → add
  `department_id` columns + indexes → add surrogate `resolution_id`/`meeting_id`
  columns → run discovery backfill over existing strings → backfill provenance +
  surrogate links → add FKs `NOT VALID` → flag unmatched/near-dup.
- **Validation (optional follow-up):** after an orphan sweep,
  `ALTER TABLE <t> VALIDATE CONSTRAINT <fk>;` promotes each FK to fully verified.
- **Rollback:** every step reversible — `DROP CONSTRAINT`, `DROP COLUMN department_id /
  resolution_id / meeting_id`, `DROP TABLE department_aliases, departments`. Raw
  `department` strings untouched → lossless rollback.
- `sql/schema.sql` updated so a fresh (any-city) bootstrap creates the same end state,
  still with **no seeded departments**.

---

## 5. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Typo/variant fragments into two departments | `pg_trgm` near-duplicate flag on create + clerk `merge_departments` (§3.8); city-agnostic |
| Concurrent ingest auto-creates same department twice | `ON CONFLICT (canonical_key) DO NOTHING` + re-select; `canonical_key` UNIQUE |
| Legacy orphan rows would fail a normal FK | `NOT VALID` constraints skip existing rows; enforced on new writes |
| Duplicate `resolution_number` makes votes→resolutions ambiguous | Surrogate `resolution_id`, earliest-match, ambiguities logged not guessed |
| Insert writes a fact before its document row | Verified order documents→chunks→facts; not possible in current pipeline |
| Dashboard vs SQL disagree on "one department" | `canonical_key` uses the dashboard's own `_dept_key_base`; shared logic |
| Unmapped `department` silently lost | Never dropped — string kept, `department_id` NULL, flagged for review |

---

## 6. Verification

- **Diagram:** Supabase schema view shows `documents`/`document_chunks` as hubs,
  `departments` as a spine across the 9 tables, `votes`/`meeting_actions` linked.
- **Discovery correctness:** after backfill, `departments` contains exactly the
  distinct real departments in the data; spot-check that no city-specific value was
  invented and that obvious `&`/`and` variants collapsed.
- **Backfill coverage:** report `count(department_id NOT NULL) / count(*)` per table;
  investigate NULL-heavy tables against `review_flags`.
- **Ingest smoke test:** dry-run ingest on one already-ingested document (re-use cached
  extraction — no new LLM calls) and confirm rows write with `department_id` populated,
  new departments auto-create, and no FK violations.
- **City-agnostic check:** confirm the migration + `schema.sql` contain zero literal
  department names.
- **Dashboard unchanged:** `_build_departments` groupings stay stable (shared key).

---

## 7. Open items requiring your input

1. **Near-duplicate threshold:** ship the `pg_trgm` similarity flag on auto-create, and
   at what similarity (default suggestion 0.6)? Or skip it and rely purely on manual
   merge?
2. **Harrisburg alias convenience (§3.6):** replay the dashboard's existing known-good
   Harrisburg merges into Harrisburg's `department_aliases` as a separate opt-in file,
   or start Harrisburg clean and let the clerk merge?
3. **`merge_departments` UI:** SQL function now; is a dashboard button in scope or a
   follow-up?
4. Add `source_file` to `vacancies` for provenance parity (optional).
5. Optional `llm_usage.query_id → query_logs.query_id` link (clean, but `query_id` is
   null/foreign for ingest-time calls — left out of core).
