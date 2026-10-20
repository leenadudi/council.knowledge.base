# Schema Relationships + Entity Dimensions — Design

**Date:** 2026-07-28
**Status:** Draft, pending user review
**Scope:** One additive, non-destructive SQL migration + small ingest changes. No table
rebuilds, no data loss, no LLM spend. **City-agnostic:** nothing about any specific
city's departments, people, or vendors is hardcoded — all three dimensions populate
themselves from ingested data.

---

## 1. Problem

The Supabase schema has ~20 tables and **exactly one** declared foreign key
(`evaluation_results.question_id → evaluation_suite.id`). Every other table renders
as an isolated box in Supabase's schema diagram. Three distinct problems underlie this:

**(a) Relationships exist in the data but are never declared.** Provenance columns
copied across the fact tables already point at real tables — Postgres just can't see it:

| Column | Actually references | Target is |
| --- | --- | --- |
| `source_chunk_id` | `document_chunks.chunk_id` | PRIMARY KEY (real pgvector table) |
| `source_file` | `documents.source_file` | already UNIQUE |
| `chunk_performance.chunk_id` | `document_chunks.chunk_id` | PRIMARY KEY |
| `votes.resolution_number` | `resolutions.resolution_number` | not unique (see §3.4) |
| `meeting_actions` (by date/number) | `meetings`, `resolutions`, `legislation` | no stable keys (see §3.4) |
| `documents.document_type` | `document_type_registry.type_name` | registry never referenced (see §3.6) |

**(b) Three entity types fragment as free text.** The codebase *already* canonicalizes
all three — but only in application logic, never as SQL identity:

| Entity | Existing canonicalizer | Graph store | SQL |
| --- | --- | --- | --- |
| Departments | `_dept_key_base` + `_DEPT_ALIASES` (`aggregator.py:265-291`) | `Department` nodes | free text in 10 tables |
| People | `normalize_person_name` (`names.py:17`); pipeline calls `upsert_council_members` (`pipeline.py:428`) | `CouncilMember` nodes | free text in 3 tables |
| Vendors | `_normalize_party` (`projects.py:72`) | — | free text in `resolutions` |

Observed drift in live data: `Bureau of Police`/`Buraeu of Police` (typo),
`Office of Equity & Compliance`/`... and Compliance`, five Lamar lease resolutions as
separate vendor strings. Without canonical keys you cannot reliably join or aggregate
an entity across budget + goals + grants + votes — the **cross-table synthesis** the
product is meant to win on.

**(c) Multi-city constraint.** This schema is the flagship for Harrisburg but is
intended to be **redeployed per city** (each city = its own database, same schema).
Every city has different departments, members, and vendors. Therefore **no entity
names, aliases, or merge rules may be hardcoded** into the schema or migration. All
dimensions must discover themselves from whatever data a given city ingests.

### Tables carrying a `department` string (10)
`expenditures`, `metrics`, `grants`, `vacancies`, `documents`, `resolutions`,
`appropriations`, `goals`, `document_chunks`, `evaluation_suite`.
(`votes`, `meetings`, `meeting_actions`, `legislation` do **not** carry `department` —
they carry *people*: `council_member`, `president`, `sponsor`.)

---

## 2. Goals & non-goals

**Goals**
- Declare every foreign key that genuinely exists in the data, so the schema diagram
  reflects the true relational model and Postgres enforces integrity.
- Add three **self-populating entity dimensions** — `departments`, `council_members`,
  `vendors` — via one uniform, city-agnostic pattern (§3.1), instantiated three times.
- Close the legislative loop: minutes → action → resolution/ordinance → votes → members.
- Do it **additively**: keep every existing column, break no insert or query path.
- **Deterministic, zero LLM spend** — canonicalization is string normalization
  (reusing the app's existing logic), not an AI resolver.
- Idempotent, re-runnable migration consistent with the existing `sql/migrate_*` style.

**Deployment model:** one database per city, identical schema. No `city_id`/tenant
column — tenancy is the deployment boundary. (Does not preclude a future shared-tenant
migration; out of scope here.)

**Non-goals**
- **Any hardcoded or seeded entity list, alias, or city-specific merge rule.** All
  dimension tables ship **empty** and fill from data.
- Multi-tenant `city_id` columns (separate DB per city instead).
- Dropping raw string columns (kept as audit trail — user decision).
- A shared time dimension; unifying resolutions/legislation/appropriations into one
  table; RLS/auth; re-ingesting or re-extracting any document.
- Deliberate non-links listed in §8 (each has a verified reason).

---

## 3. Design

### 3.1 The entity-dimension pattern (defined once, instantiated three times)

Each dimension is a pair of tables plus a deterministic resolver:

```sql
CREATE TABLE IF NOT EXISTS <entity> (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,   -- string as first seen in data
    canonical_key  VARCHAR(150) NOT NULL UNIQUE,   -- deterministic normalized key
    active         BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE when merged away (§3.9)
    created_at     TIMESTAMP DEFAULT NOW()
    -- plus per-entity extras, below
);
CREATE TABLE IF NOT EXISTS <entity>_aliases (
    id          SERIAL PRIMARY KEY,
    <entity>_id INTEGER NOT NULL REFERENCES <entity>(id) ON DELETE CASCADE,
    alias       VARCHAR(150) NOT NULL UNIQUE       -- every raw variant seen in data
);
```

**Resolver** (`resolve_<entity>_id(raw) -> id | None`), concurrency-safe:
1. Exact-match raw string against `<entity>_aliases.alias` → return `department_id`.
2. Else compute `canonical_key`; match `<entity>.canonical_key` → return id, record raw
   string as new alias.
3. Else — **auto-create mode** (default): `INSERT ... ON CONFLICT (canonical_key) DO
   NOTHING` + re-select (race-safe), record alias, return id.
   **Match-only mode** (per-column, see §3.2): return `NULL` and write a `review_flags`
   row instead of creating — used where the raw string might not denote that entity
   type at all.

**Near-duplicate guard** (all three dims): on auto-create, if `pg_trgm`
`similarity(new_key, existing_key) > 0.6`, write `review_flags`
(`reason='possible_duplicate_<entity>'`, detail naming both) so a clerk can confirm a
merge — catches `buraeu of police` → `bureau of police` and `wanda r d williams` →
`wanda williams` without hardcoding anything. Threshold is one tunable constant.

**The three instantiations** (keys reuse logic the app already trusts, so SQL and
dashboard/graph never disagree):

| Dimension | `canonical_key` derivation | Extras |
| --- | --- | --- |
| `departments` | `_dept_key_base` logic: lowercase, ` and `→` & `, strip leading `bureau of/department of/office of/the `, collapse punctuation | `dept_type` (inferred from prefix), `parent_id → departments(id)` for later rollups |
| `council_members` | `lower(normalize_person_name(raw))` — strips honorifics/titles (`names.py`) | — |
| `vendors` | `_normalize_party(raw)` — lowercase, strip corporate suffixes (LLC/Inc/Corp…), collapse punctuation | — |

### 3.2 Dimension FK columns on fact tables

All nullable, added beside the kept raw string columns:

| New column | On tables | Resolver mode |
| --- | --- | --- |
| `department_id → departments(id)` | the 10 department-bearing tables (§1) | auto-create |
| `member_id → council_members(id)` | `votes` | auto-create |
| `president_id → council_members(id)` | `meetings` | auto-create |
| `sponsor_id → council_members(id)` | `legislation` | **match-only** — sponsor strings can be committees ("Public Works Committee"), which must not become members |
| `vendor_id → vendors(id)` | `resolutions` | auto-create |

Each gets an index. Unresolved values stay `NULL` with the raw string intact — no data
ever dropped.

### 3.3 Provenance foreign keys

Pipeline insert order is **documents → chunks → facts** (`pipeline.py:204` / `:298` /
`:320+`), so these never fire against a missing parent on new writes.

| Constraint | Tables |
| --- | --- |
| `source_file → documents(source_file)` ON DELETE SET NULL | expenditures, metrics, grants, vacancies*, resolutions, votes, meetings, meeting_actions, legislation, appropriations, goals, document_chunks |
| `source_chunk_id → document_chunks(chunk_id)` ON DELETE SET NULL | expenditures, metrics, grants, vacancies, resolutions, votes, meetings, meeting_actions, legislation, appropriations, goals |
| `chunk_id → document_chunks(chunk_id)` ON DELETE CASCADE | chunk_performance |

```sql
ALTER TABLE <t> ADD CONSTRAINT fk_<t>_source_file
    FOREIGN KEY (source_file) REFERENCES documents(source_file)
    ON DELETE SET NULL NOT VALID;
```

- **`NOT VALID`**: FK appears in the diagram and is enforced on new/updated rows
  immediately, but legacy rows aren't scanned — a pre-existing orphan can't fail the
  migration. `VALIDATE CONSTRAINT` later (§4) after an orphan sweep.
- **`ON DELETE SET NULL`**: re-ingesting/deleting a chunk or document nulls the link
  instead of cascading deletes into hard-won fact rows.
- `vacancies*` gains a `source_file` column in this migration (§7.4); backfill leaves it
  NULL for pre-existing vacancy rows.

### 3.4 Legislative cross-links (closes the loop)

Natural keys are dirty (`resolution_number` can repeat — no de-dup at insert,
`sql_store.py:244`; `meetings` has only `meeting_date`), so all links are nullable
surrogate FKs, backfilled earliest-match (`MIN(id)`), ambiguous/unmatched → logged to
`review_flags`, never guessed:

| New column | Backfill match |
| --- | --- |
| `votes.resolution_id → resolutions(id)` | `resolution_number` |
| `votes.meeting_id → meetings(id)` | shared `source_file` (votes extracted from a minutes doc link to that session; votes from standalone resolution PDFs stay NULL) |
| `meeting_actions.meeting_id → meetings(id)` | `meeting_date` |
| `meeting_actions.resolution_id → resolutions(id)` | `item_number` where `item_type='resolution'` |
| `meeting_actions.legislation_id → legislation(id)` | `item_number` where `item_type='ordinance'` |

Result: minutes → action → the actual resolution/ordinance record → its votes → the
members who cast them, all traversable in SQL.

### 3.5 `meeting_attendance` junction

`meetings.members_present_names` / `members_absent_names` are comma-joined TEXT — real
per-member facts trapped in strings. Normalize into:

```sql
CREATE TABLE IF NOT EXISTS meeting_attendance (
    id          SERIAL PRIMARY KEY,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    member_id   INTEGER NOT NULL REFERENCES council_members(id),
    status      VARCHAR(10) NOT NULL CHECK (status IN ('present','absent')),
    UNIQUE (meeting_id, member_id)
);
```

Backfill parses the comma lists through `normalize_person_name` and the member
resolver (auto-create — roll-call names are members by definition). **Built-in
validator:** for each meeting, `COUNT(*) WHERE status='present'` must equal the
existing `members_present` INTEGER; mismatch → `review_flags` row, raw TEXT kept
untouched either way. Ingest populates the junction directly going forward; the TEXT
columns remain as audit trail.

### 3.6 `document_type_registry` finally referenced

The registry was built "extensible for future document types" and then never linked.

- Add `UNIQUE (type_name)`.
- FKs: `documents.document_type → document_type_registry(type_name)` and
  `document_chunks.document_type → document_type_registry(type_name)`, both
  ON UPDATE CASCADE ON DELETE SET NULL, `NOT VALID`.
- **Auto-register discovery** (same philosophy as dimensions): `record_document` and the
  chunk writer ensure the type exists first (`INSERT ... ON CONFLICT DO NOTHING`,
  `added_by='auto'`) — a novel type registers itself instead of violating the FK.
- Backfill: insert all distinct observed `document_type` values before adding the FKs.

### 3.7 Discovery-based population & backfill (no seeds, city-agnostic)

The migration ships all dimension/junction tables **empty**. Population:

1. **Backfill = discovery over existing strings.** Run the resolvers over the distinct
   raw values already in the tables (departments from 10 tables; members from
   `votes.council_member`, `meetings.president`, attendance lists; vendors from
   `resolutions.vendor`; sponsor match-only pass afterwards), then set the FK columns
   by alias join. Even Harrisburg's dimensions are derived from Harrisburg's own data —
   identical to how a brand-new city populates.
2. **Ongoing ingest** calls the same resolvers at insert time (§3.8).
3. Remaining unmatched strings → `review_flags`; add one alias row and re-run — the
   backfill is idempotent and converges.

**Harrisburg alias convenience — in scope, as a separate opt-in file.** The dashboard's
known-good Harrisburg merges (`_DEPT_ALIASES`, `aggregator.py:265`) are replayed into
*Harrisburg's* `department_aliases` via `sql/seed_harrisburg_department_aliases.sql` —
run only against Harrisburg's database, never shipped in the generic schema.

### 3.8 Ingest code changes (contained)

In `sql_store.py`: the three resolvers + `ensure_document_type`. Insert paths set the
new FK columns beside the raw strings they already write:

- `insert_expenditure/metric/grant/vacancy/resolution/goal/..._rows` → `department_id`
- `insert_vote_rows` → `member_id`, `resolution_id`, `meeting_id` (when resolvable)
- `insert_meeting_rows` → `president_id`; attendance junction rows
- `insert_meeting_action_rows` → `meeting_id`, `resolution_id`/`legislation_id`
- `insert_legislation_rows` → `sponsor_id` (match-only)
- `insert_resolution_rows` → `vendor_id`
- `record_document` → `department_id`, ensured `document_type`
- `insert_vacancy_rows` gains `source_file` (call site `pipeline.py:340` already has it)

Resolvers cache in-process per ingest run (dict), so the added lookups are negligible.
Raw strings continue to be written exactly as today — nothing downstream breaks.

### 3.9 Clerk merge operations (how variants get reconciled)

Reconciling what normalization can't catch is a **data action, not code** — one
function per dimension (`merge_department`, `merge_member`, `merge_vendor`):

1. Repoint `<entity>_aliases.department_id` (etc.) from `dup_id` → `keep_id`.
2. `UPDATE` every FK column referencing the dimension (`department_id` ×10 tables;
   `member_id`/`president_id`/`sponsor_id` + `meeting_attendance.member_id`;
   `vendor_id`) from `dup_id` → `keep_id`.
3. Set `active = FALSE` on `dup_id` (kept for audit, hidden from pickers).

SQL functions in scope now; dashboard UI buttons are a follow-up (§7.3).

---

## 4. Migration mechanics

- **One file:** `sql/migrate_2026_07_28_schema_relationships.sql`, idempotent
  (`IF NOT EXISTS`, guarded `ADD CONSTRAINT`, `ON CONFLICT DO NOTHING`).
- **Order:** enable `pg_trgm` → create 3 dimension pairs + `meeting_attendance`
  (empty) → registry `UNIQUE` → add all FK columns + indexes (§3.2, §3.4,
  `vacancies.source_file`) → discovery backfill (dimensions, then FK columns, then
  attendance parse, then registry types) → add provenance + registry FKs `NOT VALID`
  → flag unmatched/near-dup/count-mismatch.
- **Fresh-bootstrap parity:** `sql/schema.sql` updated to the same end state (still
  zero seeded entities); `vector_store.py` DDL gains `department_id` on
  `document_chunks` so new deployments match.
- **Validation (optional follow-up):** after an orphan sweep,
  `ALTER TABLE <t> VALIDATE CONSTRAINT <fk>;` promotes each FK to fully verified.
- **Rollback:** every step reversible — drop constraints, drop added columns, drop the
  new tables. Raw strings untouched → lossless.

---

## 5. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Typo/variant fragments a dimension | trgm near-dup flag on create + clerk merge ops (§3.9); city-agnostic |
| Concurrent ingest double-creates an entity | `ON CONFLICT (canonical_key)` + re-select; UNIQUE keys |
| Committee string becomes a "member" via sponsor | `sponsor_id` is match-only — never auto-creates (§3.2) |
| Attendance comma-parse breaks on odd names | count validator vs `members_present` + flags; raw TEXT kept |
| Legacy orphan rows would fail a normal FK | `NOT VALID`; enforced on new writes only |
| Duplicate `resolution_number` / reused dates make links ambiguous | surrogate FKs, earliest-match, ambiguities logged not guessed |
| Novel document type violates registry FK | auto-register before write (§3.6) |
| Fact written before its parent document | verified order documents→chunks→facts |
| Dashboard vs SQL disagree on identity | canonical keys reuse the app's own normalizers |
| Resolver latency on bulk insert | in-process cache per run; lookups are indexed exact matches |
| Unmapped strings silently lost | never dropped — raw kept, FK NULL, flagged |

---

## 6. Verification

- **Diagram:** Supabase schema view shows six hubs — `documents`, `document_chunks`,
  `departments`, `council_members`, `meetings`, `resolutions` — with every fact table
  connected; ~45 declared relationships vs 1 today.
- **Discovery correctness:** dimensions contain exactly the distinct real entities in
  the data; `&`/`and` variants collapsed; zero literal entity names anywhere in the
  migration or `schema.sql` (grep check).
- **Backfill coverage:** `count(fk NOT NULL)/count(*)` per FK column; NULL-heavy
  columns reconciled against `review_flags`.
- **Loop check:** for a known session, traverse minutes → `meeting_actions` →
  `resolutions` → `votes` → `council_members` in one SQL query.
- **Vendor check:** the five Lamar lease resolutions share one `vendor_id`.
- **Attendance check:** per-meeting present-count equals `members_present` (or flagged).
- **Ingest smoke test:** dry-run one already-ingested document (cached extraction — no
  new LLM calls): FK columns populate, novel entities auto-create, no violations.
- **Dashboard unchanged:** `_build_departments` groupings stable (shared key logic).

---

## 7. Decisions (resolved)

1. **Near-duplicate flag — YES**, `pg_trgm similarity > 0.6` on auto-create, one
   tunable constant, applied to all three dimensions.
2. **Harrisburg alias convenience — YES**, separate opt-in
   `sql/seed_harrisburg_department_aliases.sql`, Harrisburg-only.
3. **Merge ops — SQL functions in scope; dashboard UI is a follow-up.**
4. **`vacancies.source_file` — ADD** for provenance parity.
5. **Full link map — YES** (user 2026-07-28: "best connected"): council_members +
   vendors dimensions, meeting_attendance junction, legislative cross-links, registry
   FKs, `evaluation_suite.department_id`.
6. **`sponsor_id` is match-only** — committee strings must not become members.

## 8. Deliberate non-links (verified, documented so the diagram stays honest)

| Candidate | Why not |
| --- | --- |
| `llm_usage.query_id → query_logs` | **Write-order hazard (verified):** usage rows are inserted *during* a query; the `query_logs` row is written at the *end* — the FK would reject every legitimate mid-query row. |
| `review_flags.source_file → documents` | **By design (verified):** parse-stage flags fire at `pipeline.py:155`, before `record_document` at `:204` — flagging documents too broken to record is the table's purpose. Soft reference, indexed. |
| `account_number` / `metric_name` / `fund` dimensions | Over-modeling — no metadata to store about them; natural grouping keys suffice within a city DB. |
| `grants ↔ resolutions` | Semantic ("Resolution 21 authorizes the DCNR grant"), not a data key — graph store territory. |
| `query_logs` JSONB internals (`chunks_retrieved` etc.) | Logs are logs — normalizing them adds write cost to every query for marginal query value. |
