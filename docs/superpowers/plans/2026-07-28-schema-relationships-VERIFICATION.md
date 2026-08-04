# 2026-07-28 Schema Relationships: Production Verification Runbook

**Purpose:** End-to-end verification of the schema relationships migration on a Supabase branch before applying to production.

**Scope:** Applies `sql/migrate_2026_07_28_schema_relationships.sql`, backfill scripts, and Harrisburg-only alias seed against a branch database clone. Verifies FK coverage, loop integrity, and dimension accuracy before production rollout.

---

## Known Limitation — Legislative Links

`votes.resolution_id` / `votes.meeting_id` and `meeting_actions.meeting_id` / `meeting_actions.resolution_id` / `meeting_actions.legislation_id` are populated by the migration's one-time SQL backfill (see "Pure-SQL legislative backfill" in `sql/migrate_2026_07_28_schema_relationships.sql`), **not** by ongoing ingest. Documents ingested after the migration has already run do not get these links automatically.

After ingesting new resolution/minutes/ordinance documents, re-run the migration's backfill — re-applying the idempotent migration (`psql "$SCRATCH_DATABASE_URL" < sql/migrate_2026_07_28_schema_relationships.sql`) refreshes the links against the table's current contents. For a brand-new city, these columns populate only once data exists **and** the migration/backfill has been run at least once against that city's database.

---

## Prerequisites

- [ ] Production DB backup initiated (Supabase dashboard → Backups).
- [ ] A Supabase branch clone of production is ready (Database > Branches > Create branch).
- [ ] Local environment: `SCRATCH_DATABASE_URL` points to the **branch** connection string (not production).
- [ ] Python environment ready: `pip install -r requirements.txt`.
- [ ] SQL files in place:
  - `sql/migrate_2026_07_28_schema_relationships.sql`
  - `sql/seed_harrisburg_department_aliases.sql`
  - `sql/schema.sql` (already in repo)
- [ ] Backfill script: `scripts/backfill_entities_2026_07_28.py`

---

## Step 1: Apply the Migration

**Important:** Ensure the app has initialized and `document_chunks` table exists before running the migration. The migration adds FK constraints to `document_chunks` only if the table has already been created by the app on first startup. If `document_chunks` does not exist:
- Either run the app first to initialize it, OR
- Run the migration after the first app start (when `document_chunks` is created by the app).

```bash
export SCRATCH_DATABASE_URL="postgresql://user:pass@branch-hostname/dbname"

# Apply the schema migration
psql "$SCRATCH_DATABASE_URL" < sql/migrate_2026_07_28_schema_relationships.sql
```

**Expected:** Migration completes without errors. All six hub tables and their fact tables are created or updated. FK constraints are marked `NOT VALID` (will be validated in Step 5).

---

## Step 2: Apply the Backfill (First Run)

```bash
python -m scripts.backfill_entities_2026_07_28
```

**Expected:**
- `departments` populated with canonical keys.
- `council_members` populated with canonical keys.
- `vendors` populated with canonical keys.
- `department_id`, `council_member_id`, `vendor_id` columns backfilled across ~10 tables.
- `review_flags` populated with near-duplicates (0.6+ similarity) and unresolved entity references.

**Note on integration tests:** `tests/storage/test_resolution_store.py` contains 3 live-Postgres integration tests that will fail until the migration is applied to that database. Once this migration is applied to the target DB (the branch, and later production), these tests will pass.

---

## Step 3: Apply Harrisburg Alias Seed

```bash
psql "$SCRATCH_DATABASE_URL" < sql/seed_harrisburg_department_aliases.sql
```

**Expected:** Up to 11 rows inserted into `department_aliases`, one per Harrisburg-specific variant (Planning Bureau, Harrisburg City Council, etc.). Remaining inserts are idempotent (ON CONFLICT DO NOTHING).

### 3.1 Per-Row Verification (do NOT rely on the lump total)

A lump "~11 rows" count can hide a silent no-op: if any `target_key` doesn't match an existing `departments.canonical_key`, that row's JOIN yields 0 rows and the seed still "succeeds." Confirm each alias actually landed:

```sql
-- Confirm every Harrisburg alias actually landed (each maps to an existing canonical department).
-- Any alias missing here means its target department did not exist in this city's data —
-- investigate (it may be a variant that never appears, e.g. council not treated as a department),
-- or reconcile via merge_department().
SELECT alias FROM department_aliases
WHERE alias IN (
  'Planning Bureau','Harrisburg City Council','City of Harrisburg City Council',
  'Department of Finance','Departnment of Finance','Park Maintenance','Parks Maintenance',
  'Department of Economic Development & Building and Housing','Codes Health Department',
  'Buraeu of Police','Bureau of Coummunicaitons'
);
-- Expect all 11. If fewer, the missing ones did not seed (their target department is absent).
```

**Note:** The `Harrisburg City Council` / `City of Harrisburg City Council` rows only land if "City Council" appears as a department in the fact tables — if they don't seed, that's expected (council actions aren't departments) and not an error.

---

## Step 4: Re-run the Backfill (Incorporation Pass)

```bash
python -m scripts.backfill_entities_2026_07_28
```

**Expected:** The backfill picks up the newly-aliased departments. Existing `department_id` links are re-validated; any records that previously mapped to aliases now map to their canonical departments via the alias registry.

---

## Step 5: Verify Schema & Relationships

Run each query against the branch database and record results.

### 5.1 Foreign Key Count

```sql
SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_type='FOREIGN KEY';
```

**Expected:** ≥ ~30 (was 1 before the migration). Result confirms multi-table FK structure is in place.

### 5.2 Department ID Coverage (per table)

For each of the following tables, run:
```sql
SELECT count(*) FILTER (WHERE department_id IS NOT NULL)::float/count(*) FROM <table_name>;
```

Tables to check:
- `meetings`
- `resolutions`
- `votes`
- `meeting_attendance`
- `meeting_actions`
- `vacancies`
- `board_members`
- `budget_items`
- `expenditures`
- `metrics`

**Expected:** 80%+ coverage for most tables (Harrisburg data is well-linked).

### 5.3 Loop Integrity Check

Verify the legislative pipeline is intact: minute-level meetings link to resolutions, which link to votes, which link to council members.

```sql
-- Spot-check: a meeting → actions → resolutions → votes → council_members
SELECT
  m.id AS meeting_id,
  COUNT(DISTINCT ma.resolution_id) AS actions_count,
  COUNT(DISTINCT v.id) AS votes_count,
  COUNT(DISTINCT v.council_member_id) AS members_voted
FROM meetings m
LEFT JOIN meeting_actions ma ON m.id = ma.meeting_id
LEFT JOIN resolutions r ON ma.resolution_id = r.id
LEFT JOIN votes v ON r.id = v.resolution_id
WHERE m.document_id IS NOT NULL
GROUP BY m.id
LIMIT 5;
```

**Expected:** Non-zero rows showing meetings → actions → resolutions → votes → members. If all counts are 0, review review_flags for unresolved entities.

### 5.4 Vendor Dimension Join

Verify vendors are correctly linked via resolutions:

```sql
SELECT count(*) FROM resolutions r
JOIN vendors v ON r.vendor_id=v.id
WHERE v.canonical_key = vendor_key('Lamar Advertising');
```

**Expected:** ≥ 1 (Lamar lease resolutions share one vendor entry).

### 5.5 Meeting Attendance Validation

Verify that per meeting, `count(*) FILTER (status='present')` equals the `members_present` column (or any discrepancies are flagged in `review_flags`):

```sql
SELECT
  m.id,
  m.members_present,
  COUNT(*) FILTER (WHERE status='present') AS actual_present,
  CASE
    WHEN COUNT(*) FILTER (WHERE status='present') = m.members_present THEN 'OK'
    ELSE 'REVIEW'
  END AS validation
FROM meetings m
LEFT JOIN meeting_attendance ma ON m.id = ma.meeting_id
GROUP BY m.id, m.members_present
HAVING COUNT(*) FILTER (WHERE status='present') <> m.members_present;
```

**Expected:** Either all rows pass (validation='OK'), or discrepancies are documented in `review_flags` (check next query).

### 5.6 Review Flags

Inspect unresolved flags to identify any entities still pending manual merge or disambiguation:

```sql
SELECT * FROM review_flags
WHERE source_file IN ('schema-migration','entity-resolver')
  AND NOT resolved
ORDER BY entity_type, source_file;
```

**Expected:** Either empty (all entities resolved), or flagged items requiring SME review. Document any action items.

---

## Step 6: Inspect the Supabase Schema Diagram

1. Open Supabase dashboard → Select the **branch** database.
2. Navigate to **SQL Editor** → **Schema** (or **Database** → **Diagrams**).
3. Visually confirm the six hub tables and their radiating fact tables:
   - **`documents`** (hub) ← `document_chunks`
   - **`document_chunks`** (hub) ← (no direct spokes, acts as bridge)
   - **`departments`** (hub) ← `department_aliases`, `vacancies`, radiates to facts via `department_id`
   - **`council_members`** (hub) ← `meeting_attendance`, `votes`, radiates via `council_member_id`
   - **`meetings`** (hub) ← `meeting_actions`, `meeting_attendance`, radiates via `meeting_id`
   - **`resolutions`** (hub) ← `votes`, `meeting_actions`, radiates via `resolution_id`; also links to `vendors` and `departments` via FK

**Expected:** Clear hub-and-spoke topology; no cycles (except intended legislative pipeline: meetings → actions → resolutions → votes → members).

---

## Step 7: Proceed to Production (Only After Branch Verification)

**Prerequisite:** All queries in Step 5 pass; schema diagram looks correct; no critical flags in review_flags.

### 7.1 Backup Production (Confirm Again)

Verify a recent backup exists in Supabase → Backups. If < 1 hour old, safe to proceed.

### 7.2 Apply Migration, Backfill, and Seed to Production

```bash
# Point to PRODUCTION (not branch)
export SCRATCH_DATABASE_URL="postgresql://user:pass@prod-hostname/dbname"

# Apply migration
psql "$SCRATCH_DATABASE_URL" < sql/migrate_2026_07_28_schema_relationships.sql

# Backfill (first pass)
python -m scripts.backfill_entities_2026_07_28

# Harrisburg alias seed
psql "$SCRATCH_DATABASE_URL" < sql/seed_harrisburg_department_aliases.sql

# Backfill (second pass)
python -m scripts.backfill_entities_2026_07_28
```

### 7.3 Smoke Test in Production

Run a subset of queries from Step 5 on production to confirm parity with branch:

```sql
-- FK count
SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_type='FOREIGN KEY';

-- Sample table coverage
SELECT count(*) FILTER (WHERE department_id IS NOT NULL)::float/count(*) FROM meetings;

-- Loop check
SELECT COUNT(*) FROM meetings m
LEFT JOIN meeting_actions ma ON m.id = ma.meeting_id
LEFT JOIN votes v ON v.resolution_id IN (SELECT id FROM resolutions r WHERE r.id = ma.resolution_id);
```

**Expected:** Same results as branch.

---

## Step 8: (Optional) Promote NOT VALID Constraints to FULLY VALID

After an orphan sweep or SME merge/dedup pass:

```sql
ALTER TABLE <table_name>
VALIDATE CONSTRAINT <constraint_name>;
```

Example:

```sql
ALTER TABLE meeting_actions VALIDATE CONSTRAINT fk_meeting_actions_resolution_id;
ALTER TABLE vacancies VALIDATE CONSTRAINT fk_vacancies_department_id;
```

**Expected:** Constraint validation completes (may take minutes on large tables). No errors = all FKs enforced.

---

## Rollback Procedure (If Needed)

If any step fails or schema is corrupted:

1. Stop the app (if running against prod).
2. Restore from the pre-migration backup: Supabase → Backups → Restore.
3. Investigate root cause (e.g., orphaned rows, app not initialized, wrong connection string).
4. Re-plan and retry.

---

## Sign-Off

- [ ] All Step 5 queries pass on branch.
- [ ] Schema diagram is correct.
- [ ] No critical review_flags remain.
- [ ] Production has current backup.
- [ ] Production migration, backfill, and seed applied successfully.
- [ ] Production smoke tests pass.
- [ ] Ready for feature team handoff.
