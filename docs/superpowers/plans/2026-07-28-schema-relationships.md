# Schema Relationships + Entity Dimensions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn ~20 unlinked Postgres tables into a well-connected relational model — declare every real foreign key, and add three self-populating entity dimensions (departments, council_members, vendors) — additively, without breaking live data or ingestion.

**Architecture:** One idempotent SQL migration adds dimension/junction tables (empty), FK columns, and provenance/legislative foreign keys (`NOT VALID` so legacy rows can't fail it). A pure-Python module computes city-agnostic canonical keys. `SQLStore` gains resolvers that auto-create dimension rows on first sight and merge functions to reconcile variants. A one-shot backfill script discovers dimensions from existing strings and populates the new FK columns. Nothing runs against a live database until the final verification task, which uses a **scratch** database, never production.

**Tech Stack:** Python 3.11, psycopg2, PostgreSQL 16 (Supabase), pytest.

## Global Constraints

- **No LLM spend.** Every step is deterministic SQL/Python. No ingestion, extraction, or Ask-test calls (owner's funds rule).
- **City-agnostic.** No specific city's department/person/vendor names appear in the migration or `sql/schema.sql`. Dimensions populate from data. (`sql/seed_harrisburg_department_aliases.sql` is the one Harrisburg-only file and is never applied to another city.)
- **Additive & non-destructive.** Keep every existing column (raw `department`/`vendor`/person strings stay as audit trail). New columns are nullable. FKs are added `NOT VALID`.
- **Never touch production.** All migration/backfill execution in this plan targets a throwaway scratch database via a `SCRATCH_DATABASE_URL` env var. Applying to the real DB is a separate, human-run step outside this plan.
- **Canonical keys must match the dashboard.** `department_key` ≡ `DashboardAggregator._dept_key_base`; `vendor_key` ≡ `dashboard.projects._normalize_party`. Parity tests guard this.
- **Follow repo conventions:** migrations are `sql/migrate_YYYY_MM_DD_*.sql` (idempotent — `IF NOT EXISTS`, guarded `ADD CONSTRAINT`, `ON CONFLICT DO NOTHING`); data scripts live in `scripts/`; tests mirror `src/` under `tests/`.
- Spec of record: `docs/superpowers/specs/2026-07-28-schema-relationships-design.md`.

---

### Task 1: Pure canonical-key functions (`entity_keys.py`)

**Files:**
- Create: `src/ingestion/entity_keys.py`
- Test: `tests/ingestion/test_entity_keys.py`

**Interfaces:**
- Consumes: `src.ingestion.names.normalize_person_name(name: str | None) -> str` (existing — strips honorifics, Title-cases, returns `""` for empty).
- Produces:
  - `department_key(name: str | None) -> str`
  - `member_key(name: str | None) -> str`
  - `vendor_key(name: str | None) -> str`
  - `infer_dept_type(name: str | None) -> str | None` (`bureau|department|office|council|other`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingestion/test_entity_keys.py
import pytest

from src.ingestion.entity_keys import (
    department_key, member_key, vendor_key, infer_dept_type,
)
from src.dashboard.aggregator import DashboardAggregator
from src.dashboard.projects import _normalize_party


@pytest.mark.parametrize("a,b", [
    ("Bureau of Police", "Buraeu of Police"),          # typo differs — NOT auto-merged
    ("Office of Equity & Compliance", "Office of Equity and Compliance"),  # &/and merge
    ("Department of Facilities & Special Projects", "Department of Facilities and Special Projects"),
])
def test_department_key_merges_and_separates(a, b):
    # &/and variants collapse; typos do not
    if "and" in a.lower() or "&" in a:
        assert department_key(a) == department_key(b)
    else:
        assert department_key(a) != department_key(b)


def test_department_key_strips_generic_prefixes():
    assert department_key("Bureau of Fire") == "fire"
    assert department_key("Department of Public Works") == "public works"
    assert department_key("The Office of Equity and Compliance") == "equity & compliance"
    assert department_key(None) == ""
    assert department_key("") == ""


def test_department_key_matches_dashboard_base():
    # Parity guard: the SQL dimension key MUST equal the dashboard's own key.
    for name in ["Bureau of Codes", "Office of Equity and Compliance",
                 "Department of Public Works", "Parks & Recreation", "City Controller"]:
        assert department_key(name) == DashboardAggregator._dept_key_base(name)


def test_member_key_strips_titles_and_lowercases():
    assert member_key("Council Member Wanda Williams") == member_key("Wanda Williams")
    assert member_key("Hon. Mr. Danielle Bowers") == "danielle bowers"
    assert member_key(None) == ""


def test_vendor_key_drops_corporate_suffixes():
    assert vendor_key("Lamar Advertising, LLC") == vendor_key("Lamar Advertising")
    assert vendor_key("McCormick Law Firm Inc.") == "mccormick law firm"
    assert vendor_key(None) == ""


def test_vendor_key_matches_dashboard():
    for name in ["Lamar Advertising, LLC", "McCormick Law Firm", "PennDot"]:
        assert vendor_key(name) == _normalize_party(name)


def test_infer_dept_type():
    assert infer_dept_type("Bureau of Fire") == "bureau"
    assert infer_dept_type("Department of Public Works") == "department"
    assert infer_dept_type("Office of Equity") == "office"
    assert infer_dept_type("Harrisburg City Council") == "council"
    assert infer_dept_type("City Controller") == "other"
    assert infer_dept_type(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ingestion/test_entity_keys.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ingestion.entity_keys'`

- [ ] **Step 3: Write the implementation**

```python
# src/ingestion/entity_keys.py
"""Deterministic, city-agnostic canonical keys for entity dimensions.

Turns a raw extracted string (department, person, or vendor) into a stable
`canonical_key`: two raw strings that denote the same real entity produce the
same key. The logic is generic US-municipal-naming normalization — it hardcodes
NO city's specific departments, people, or vendors.

Keys MUST match the dashboard's existing canonicalization so the SQL dimension
and the dashboard never disagree:
  department_key <-> DashboardAggregator._dept_key_base (dashboard/aggregator.py)
  vendor_key     <-> dashboard.projects._normalize_party (dashboard/projects.py)
  member_key     wraps ingestion.names.normalize_person_name
The parity tests in tests/ingestion/test_entity_keys.py guard against drift; if
you change the dashboard functions, change these together.
"""
from __future__ import annotations

import re

from src.ingestion.names import normalize_person_name

_DEPT_PREFIXES = ("bureau of ", "department of ", "office of ", "the ")
_VENDOR_SUFFIX_RE = re.compile(
    r"[,\.]?\s*\b(llc|l\.l\.c|inc|incorporated|co|company|corp|corporation|lp|llp|pllc)\b\.?",
    re.I,
)


def department_key(name: str | None) -> str:
    """Canonical key for a department. Mirrors DashboardAggregator._dept_key_base."""
    k = (name or "").strip().lower().replace(" and ", " & ")
    for p in _DEPT_PREFIXES:
        if k.startswith(p):
            k = k[len(p):]
            break
    return re.sub(r"[^a-z0-9&]+", " ", k).strip()


def member_key(name: str | None) -> str:
    """Canonical key for a council member — strips honorifics, then lowercases."""
    return normalize_person_name(name).lower().strip()


def vendor_key(name: str | None) -> str:
    """Canonical key for a vendor/party. Mirrors dashboard.projects._normalize_party."""
    s = (name or "").lower()
    s = _VENDOR_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def infer_dept_type(name: str | None) -> str | None:
    """Best-effort department type from the generic prefix. City-agnostic."""
    if not name:
        return None
    k = name.strip().lower()
    if k.startswith("bureau of"):
        return "bureau"
    if k.startswith("department of"):
        return "department"
    if k.startswith("office of"):
        return "office"
    if "council" in k:
        return "council"
    return "other"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_entity_keys.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/entity_keys.py tests/ingestion/test_entity_keys.py
git commit -m "feat(schema): city-agnostic canonical-key functions for entity dimensions"
```

---

### Task 2: The migration SQL (schema structure)

**Files:**
- Create: `sql/migrate_2026_07_28_schema_relationships.sql`
- Modify: `sql/schema.sql` (append the same end-state so a fresh bootstrap matches)
- Modify: `src/storage/vector_store.py:37-49` (add `department_id` to the `document_chunks` DDL)
- Test: `tests/storage/test_migration_relationships.py`

**Interfaces:**
- Produces (schema objects later tasks rely on): tables `departments`, `department_aliases`, `council_members`, `council_member_aliases`, `vendors`, `vendor_aliases`, `meeting_attendance`; FK columns `department_id` (10 tables), `member_id`/`president_id`/`sponsor_id`, `vendor_id`, `resolution_id`/`meeting_id`/`legislation_id`; `vacancies.source_file`; `pg_trgm` extension; `UNIQUE(document_type_registry.type_name)`.

- [ ] **Step 1: Write the migration file**

```sql
-- sql/migrate_2026_07_28_schema_relationships.sql
-- Additive, idempotent. Declares real FKs + three self-populating entity
-- dimensions. Ships all dimension tables EMPTY (populated by scripts/backfill_
-- entities_2026_07_28.py). Safe to re-run.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---- Entity dimensions (created empty; no seed rows) -----------------------
CREATE TABLE IF NOT EXISTS departments (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,
    canonical_key  VARCHAR(120) NOT NULL UNIQUE,
    dept_type      VARCHAR(30),
    parent_id      INTEGER REFERENCES departments(id),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS department_aliases (
    id            SERIAL PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    alias         VARCHAR(150) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_department_aliases_alias ON department_aliases(LOWER(alias));

CREATE TABLE IF NOT EXISTS council_members (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,
    canonical_key  VARCHAR(150) NOT NULL UNIQUE,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS council_member_aliases (
    id        SERIAL PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES council_members(id) ON DELETE CASCADE,
    alias     VARCHAR(150) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_member_aliases_alias ON council_member_aliases(LOWER(alias));

CREATE TABLE IF NOT EXISTS vendors (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(200) NOT NULL UNIQUE,
    canonical_key  VARCHAR(200) NOT NULL UNIQUE,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS vendor_aliases (
    id        SERIAL PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    alias     VARCHAR(200) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_vendor_aliases_alias ON vendor_aliases(LOWER(alias));

-- ---- department_id on the 10 department-bearing tables ---------------------
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','documents','resolutions',
    'appropriations','goals','document_chunks','evaluation_suite'
  ] LOOP
    EXECUTE format(
      'ALTER TABLE %I ADD COLUMN IF NOT EXISTS department_id INTEGER REFERENCES departments(id)', t);
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS idx_%s_department_id ON %I(department_id)', t, t);
  END LOOP;
END $$;

-- ---- people + vendor FK columns on the tables that carry those strings -----
ALTER TABLE votes       ADD COLUMN IF NOT EXISTS member_id    INTEGER REFERENCES council_members(id);
ALTER TABLE meetings    ADD COLUMN IF NOT EXISTS president_id INTEGER REFERENCES council_members(id);
ALTER TABLE legislation ADD COLUMN IF NOT EXISTS sponsor_id   INTEGER REFERENCES council_members(id);
ALTER TABLE resolutions ADD COLUMN IF NOT EXISTS vendor_id    INTEGER REFERENCES vendors(id);
CREATE INDEX IF NOT EXISTS idx_votes_member_id      ON votes(member_id);
CREATE INDEX IF NOT EXISTS idx_meetings_president_id ON meetings(president_id);
CREATE INDEX IF NOT EXISTS idx_legislation_sponsor  ON legislation(sponsor_id);
CREATE INDEX IF NOT EXISTS idx_resolutions_vendor_id ON resolutions(vendor_id);

-- ---- legislative surrogate-FK columns --------------------------------------
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS resolution_id  INTEGER REFERENCES resolutions(id);
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS meeting_id     INTEGER REFERENCES meetings(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS meeting_id     INTEGER REFERENCES meetings(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS resolution_id  INTEGER REFERENCES resolutions(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS legislation_id INTEGER REFERENCES legislation(id);
CREATE INDEX IF NOT EXISTS idx_votes_resolution_id       ON votes(resolution_id);
CREATE INDEX IF NOT EXISTS idx_votes_meeting_id          ON votes(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_meeting   ON meeting_actions(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_res       ON meeting_actions(resolution_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_leg       ON meeting_actions(legislation_id);

-- ---- vacancies provenance parity: gains source_file ------------------------
ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS source_file VARCHAR(255);

-- ---- meeting_attendance junction -------------------------------------------
CREATE TABLE IF NOT EXISTS meeting_attendance (
    id         SERIAL PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    member_id  INTEGER NOT NULL REFERENCES council_members(id),
    status     VARCHAR(10) NOT NULL CHECK (status IN ('present','absent')),
    UNIQUE (meeting_id, member_id)
);

-- ---- document_type_registry becomes a real FK target -----------------------
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_doc_type_registry_name') THEN
    ALTER TABLE document_type_registry ADD CONSTRAINT uq_doc_type_registry_name UNIQUE (type_name);
  END IF;
END $$;

-- ---- Pure-SQL legislative backfill (earliest-match; ambiguities logged) -----
UPDATE votes v SET resolution_id = r.id
FROM (SELECT resolution_number, MIN(id) AS id FROM resolutions
      WHERE resolution_number IS NOT NULL GROUP BY resolution_number) r
WHERE v.resolution_number = r.resolution_number AND v.resolution_id IS NULL;

UPDATE votes v SET meeting_id = m.id
FROM (SELECT source_file, MIN(id) AS id FROM meetings
      WHERE source_file IS NOT NULL GROUP BY source_file) m
WHERE v.source_file = m.source_file AND v.meeting_id IS NULL;

UPDATE meeting_actions a SET meeting_id = m.id
FROM (SELECT meeting_date, MIN(id) AS id FROM meetings
      WHERE meeting_date IS NOT NULL GROUP BY meeting_date) m
WHERE a.meeting_date = m.meeting_date AND a.meeting_id IS NULL;

UPDATE meeting_actions a SET resolution_id = r.id
FROM (SELECT resolution_number, MIN(id) AS id FROM resolutions
      WHERE resolution_number IS NOT NULL GROUP BY resolution_number) r
WHERE a.item_type = 'resolution' AND a.item_number = r.resolution_number
  AND a.resolution_id IS NULL;

UPDATE meeting_actions a SET legislation_id = l.id
FROM (SELECT bill_number, MIN(id) AS id FROM legislation
      WHERE bill_number IS NOT NULL GROUP BY bill_number) l
WHERE a.item_type ILIKE 'ordinance' AND a.item_number = l.bill_number
  AND a.legislation_id IS NULL;

-- Log resolution_numbers that appear on >1 resolution row (ambiguous links).
INSERT INTO review_flags (source_file, stage, reason, detail)
SELECT 'schema-migration', 'validate', 'ambiguous_resolution_number',
       'resolution_number ' || resolution_number || ' maps to ' || COUNT(*) || ' rows'
FROM resolutions WHERE resolution_number IS NOT NULL
GROUP BY resolution_number HAVING COUNT(*) > 1;
-- Same pattern applies to duplicate meeting_date and bill_number; add analogous
-- INSERT...SELECT...HAVING COUNT(*)>1 blocks for meetings(meeting_date) and
-- legislation(bill_number) so every earliest-match ambiguity is visible.

-- ---- Provenance foreign keys, NOT VALID (won't fail on legacy orphans) ------
-- source_file -> documents(source_file); ON DELETE SET NULL.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','resolutions','votes','meetings',
    'meeting_actions','legislation','appropriations','goals','document_chunks'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_'||t||'_source_file') THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT fk_%s_source_file FOREIGN KEY (source_file) '
        'REFERENCES documents(source_file) ON DELETE SET NULL NOT VALID', t, t);
    END IF;
  END LOOP;
END $$;

-- source_chunk_id -> document_chunks(chunk_id); ON DELETE SET NULL.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','resolutions','votes','meetings',
    'meeting_actions','legislation','appropriations','goals'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_'||t||'_source_chunk') THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT fk_%s_source_chunk FOREIGN KEY (source_chunk_id) '
        'REFERENCES document_chunks(chunk_id) ON DELETE SET NULL NOT VALID', t, t);
    END IF;
  END LOOP;
END $$;

-- chunk_performance -> document_chunks (CASCADE); document_chunks -> documents;
-- document_type FKs -> registry (both NOT VALID / SET NULL).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_chunk_perf_chunk') THEN
    ALTER TABLE chunk_performance ADD CONSTRAINT fk_chunk_perf_chunk
      FOREIGN KEY (chunk_id) REFERENCES document_chunks(chunk_id) ON DELETE CASCADE NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_chunks_doc') THEN
    ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_doc
      FOREIGN KEY (source_file) REFERENCES documents(source_file) ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_documents_doctype') THEN
    ALTER TABLE documents ADD CONSTRAINT fk_documents_doctype
      FOREIGN KEY (document_type) REFERENCES document_type_registry(type_name)
      ON UPDATE CASCADE ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_chunks_doctype') THEN
    ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_doctype
      FOREIGN KEY (document_type) REFERENCES document_type_registry(type_name)
      ON UPDATE CASCADE ON DELETE SET NULL NOT VALID;
  END IF;
END $$;
```

- [ ] **Step 2: Mirror into fresh-bootstrap DDL**

In `src/storage/vector_store.py`, add `department_id INTEGER,` to the `CREATE TABLE IF NOT EXISTS document_chunks (...)` block (after `year INTEGER,`) so new deployments create the column. In `sql/schema.sql`, append the full contents of the migration (the `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` statements are safe to include verbatim) so a from-scratch bootstrap reaches the same end state, still with zero seeded dimension rows.

- [ ] **Step 3: Write the migration integration test**

```python
# tests/storage/test_migration_relationships.py
"""Applies the migration to a SCRATCH database and asserts the schema shape.
Skipped unless SCRATCH_DATABASE_URL is set. NEVER point this at production."""
import os
import pathlib
import psycopg2
import pytest

SCRATCH = os.getenv("SCRATCH_DATABASE_URL")
pytestmark = pytest.mark.skipif(not SCRATCH, reason="SCRATCH_DATABASE_URL not set")
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture()
def conn():
    c = psycopg2.connect(SCRATCH)
    yield c
    c.close()


def _apply(conn, path):
    with conn.cursor() as cur:
        cur.execute(path.read_text())
    conn.commit()


def test_migration_is_idempotent_and_creates_links(conn):
    # Minimal base schema first, then the migration twice (idempotency check).
    _apply(conn, ROOT / "sql" / "schema.sql")
    from src.storage.vector_store import VectorStore  # creates document_chunks
    VectorStore(_settings_for(SCRATCH)).ensure_collection()
    mig = ROOT / "sql" / "migrate_2026_07_28_schema_relationships.sql"
    _apply(conn, mig)
    _apply(conn, mig)  # second run must not error

    with conn.cursor() as cur:
        for tbl in ("departments", "department_aliases", "council_members",
                    "vendors", "meeting_attendance"):
            cur.execute("SELECT to_regclass(%s)", (tbl,))
            assert cur.fetchone()[0] is not None, tbl
        cur.execute("""SELECT COUNT(*) FROM information_schema.table_constraints
                       WHERE constraint_type='FOREIGN KEY' AND constraint_schema='public'""")
        assert cur.fetchone()[0] >= 30
        # dimensions ship empty
        cur.execute("SELECT COUNT(*) FROM departments")
        assert cur.fetchone()[0] == 0


def _settings_for(url):
    from src.config import get_settings
    s = get_settings()
    s.database_url = url
    return s
```

- [ ] **Step 4: Run the test against a scratch DB**

Run: `SCRATCH_DATABASE_URL=postgresql://localhost/kb_scratch pytest tests/storage/test_migration_relationships.py -v`
(Create the scratch DB first: `createdb kb_scratch`. If no local Postgres, use a Supabase branch URL — never the production URL.)
Expected: PASS. If `SCRATCH_DATABASE_URL` is unset, the test is skipped (green).

- [ ] **Step 5: Commit**

```bash
git add sql/migrate_2026_07_28_schema_relationships.sql sql/schema.sql \
        src/storage/vector_store.py tests/storage/test_migration_relationships.py
git commit -m "feat(schema): additive migration — entity dimensions, provenance + legislative FKs"
```

---

### Task 3: Entity resolvers on `SQLStore` (auto-create + near-dup flag)

**Files:**
- Modify: `src/storage/sql_store.py` (add resolver methods + module config near the top of the class)
- Test: `tests/storage/test_entity_resolvers.py`

**Interfaces:**
- Consumes: `department_key`/`member_key`/`vendor_key`/`infer_dept_type` (Task 1); `self.cursor()` (existing context manager yielding a RealDict cursor).
- Produces:
  - `SQLStore.resolve_department_id(name, *, create=True) -> int | None`
  - `SQLStore.resolve_member_id(name, *, create=True) -> int | None`
  - `SQLStore.resolve_vendor_id(name, *, create=True) -> int | None`
  - `SQLStore.ensure_document_type(type_name) -> None`
  - Internal: `_resolve_entity(...)`, `_flag(cur, stage, reason, detail)`, `_flag_near_dup(cur, table, key)`

- [ ] **Step 1: Write the failing tests** (fake cursor — no DB)

```python
# tests/storage/test_entity_resolvers.py
from contextlib import contextmanager
from src.storage.sql_store import SQLStore


class FakeCursor:
    """Matches queued (needle, result) pairs by SQL substring; records executes."""
    def __init__(self, script):
        self.script = list(script)     # list of (needle, fetchone_result)
        self.executed = []
        self._result = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._result = None
        for i, (needle, res) in enumerate(self.script):
            if needle in sql:
                self._result = res
                self.script.pop(i)
                break

    def fetchone(self):
        return self._result


def _store_with(script):
    store = SQLStore.__new__(SQLStore)          # bypass connect()
    cur = FakeCursor(script)

    @contextmanager
    def _cursor():
        yield cur
    store.cursor = _cursor
    store._fake_cursor = cur
    return store


def test_resolver_returns_existing_via_alias():
    store = _store_with([("FROM department_aliases", {"id": 7})])
    assert store.resolve_department_id("Bureau of Fire") == 7


def test_resolver_returns_existing_via_key_and_records_alias():
    store = _store_with([
        ("FROM department_aliases", None),               # no alias yet
        ("FROM departments WHERE canonical_key", {"id": 12}),
        ("INSERT INTO department_aliases", None),         # alias recorded
    ])
    assert store.resolve_department_id("Bureau of Fire ") == 12
    assert any("INSERT INTO department_aliases" in s for s, _ in store._fake_cursor.executed)


def test_resolver_auto_creates_and_flags_near_dup():
    store = _store_with([
        ("FROM department_aliases", None),
        ("FROM departments WHERE canonical_key", None),
        ("similarity", {"canonical_name": "Bureau of Police"}),  # near-dup found
        ("INSERT INTO review_flags", None),
        ("INSERT INTO departments", {"id": 99}),
        ("INSERT INTO department_aliases", None),
    ])
    assert store.resolve_department_id("Buraeu of Police") == 99
    flagged = [s for s, _ in store._fake_cursor.executed if "review_flags" in s]
    assert flagged, "near-duplicate should have been flagged"


def test_resolver_match_only_returns_none_and_flags():
    store = _store_with([
        ("FROM council_member_aliases", None),
        ("FROM council_members WHERE canonical_key", None),
        ("INSERT INTO review_flags", None),
    ])
    assert store.resolve_member_id("Public Works Committee", create=False) is None


def test_empty_name_returns_none_without_touching_db():
    store = _store_with([])
    assert store.resolve_department_id("") is None
    assert store._fake_cursor.executed == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/storage/test_entity_resolvers.py -v`
Expected: FAIL — `AttributeError: 'SQLStore' object has no attribute 'resolve_department_id'`

- [ ] **Step 3: Implement the resolvers** (add to `SQLStore` in `src/storage/sql_store.py`)

```python
from src.ingestion.entity_keys import (
    department_key, member_key, vendor_key, infer_dept_type,
)

# alias-table FK column per dimension table
_ALIAS_FK = {
    "departments": ("department_aliases", "department_id"),
    "council_members": ("council_member_aliases", "member_id"),
    "vendors": ("vendor_aliases", "vendor_id"),
}
_NEAR_DUP_THRESHOLD = 0.6  # pg_trgm similarity; one tunable constant


def _flag(self, cur, stage, reason, detail):
    cur.execute(
        "INSERT INTO review_flags (source_file, stage, reason, detail) "
        "VALUES ('entity-resolver', %s, %s, %s)", (stage, reason, detail))

def _flag_near_dup(self, cur, table, key, raw):
    cur.execute(
        f"SELECT canonical_name FROM {table} "
        f"WHERE similarity(canonical_key, %s) > %s AND canonical_key <> %s "
        f"ORDER BY similarity(canonical_key, %s) DESC LIMIT 1",
        (key, _NEAR_DUP_THRESHOLD, key, key))
    row = cur.fetchone()
    if row:
        self._flag(cur, "validate", f"possible_duplicate_{table[:-1]}",
                   f"'{raw}' resembles existing '{row['canonical_name']}'")

def _resolve_entity(self, table, raw, key, display, *, create, extra_cols=None):
    if not key:
        return None
    alias_table, fk = _ALIAS_FK[table]
    with self.cursor() as cur:
        cur.execute(
            f"SELECT {fk} AS id FROM {alias_table} WHERE LOWER(alias) = LOWER(%s)", (raw,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute(f"SELECT id FROM {table} WHERE canonical_key = %s", (key,))
        row = cur.fetchone()
        if row:
            cur.execute(
                f"INSERT INTO {alias_table} ({fk}, alias) VALUES (%s, %s) "
                f"ON CONFLICT (alias) DO NOTHING", (row["id"], raw))
            return row["id"]
        if not create:
            self._flag(cur, "validate", f"unmapped_{table[:-1]}", raw)
            return None
        self._flag_near_dup(cur, table, key, raw)
        cols = ["canonical_name", "canonical_key"] + list((extra_cols or {}).keys())
        vals = [display, key] + list((extra_cols or {}).values())
        ph = ", ".join(["%s"] * len(vals))
        cur.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) "
            f"ON CONFLICT (canonical_key) DO NOTHING RETURNING id", vals)
        row = cur.fetchone()
        if row is None:  # concurrent create won the race
            cur.execute(f"SELECT id FROM {table} WHERE canonical_key = %s", (key,))
            row = cur.fetchone()
        entity_id = row["id"]
        cur.execute(
            f"INSERT INTO {alias_table} ({fk}, alias) VALUES (%s, %s) "
            f"ON CONFLICT (alias) DO NOTHING", (entity_id, raw))
        return entity_id

def resolve_department_id(self, name, *, create=True):
    return self._resolve_entity(
        "departments", name, department_key(name), (name or "").strip(),
        create=create, extra_cols={"dept_type": infer_dept_type(name)})

def resolve_member_id(self, name, *, create=True):
    from src.ingestion.names import normalize_person_name
    display = normalize_person_name(name)
    return self._resolve_entity(
        "council_members", name, member_key(name), display, create=create)

def resolve_vendor_id(self, name, *, create=True):
    return self._resolve_entity(
        "vendors", name, vendor_key(name), (name or "").strip(), create=create)

def ensure_document_type(self, type_name):
    if not type_name:
        return
    with self.cursor() as cur:
        cur.execute(
            "INSERT INTO document_type_registry (type_name, display_name, added_by) "
            "VALUES (%s, %s, 'auto') ON CONFLICT (type_name) DO NOTHING",
            (type_name, type_name))
```

> Note: methods are shown module-style for readability; place each as a method on the `SQLStore` class (indented under `class SQLStore`, `self` first arg). `_ALIAS_FK` and `_NEAR_DUP_THRESHOLD` go at module level.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/storage/test_entity_resolvers.py -v`
Expected: PASS (all 5)

- [ ] **Step 5: Commit**

```bash
git add src/storage/sql_store.py tests/storage/test_entity_resolvers.py
git commit -m "feat(schema): entity resolvers with auto-create + pg_trgm near-dup flag"
```

---

### Task 4: Merge functions (clerk reconciliation)

**Files:**
- Modify: `src/storage/sql_store.py`
- Test: `tests/storage/test_entity_merge.py`

**Interfaces:**
- Produces: `merge_department(dup_id, keep_id)`, `merge_member(dup_id, keep_id)`, `merge_vendor(dup_id, keep_id)` — repoint aliases + all referencing FK columns, then set `active=FALSE` on `dup_id`.

- [ ] **Step 1: Write the failing tests** (fake cursor asserts the exact UPDATEs)

```python
# tests/storage/test_entity_merge.py
from tests.storage.test_entity_resolvers import _store_with  # reuse fake harness


def _executed(store):
    return [s for s, _ in store._fake_cursor.executed]


def test_merge_department_repoints_all_ten_tables_and_deactivates():
    store = _store_with([])
    store.merge_department(dup_id=5, keep_id=2)
    sql = " ".join(_executed(store))
    assert "UPDATE department_aliases SET department_id = 2" in sql
    for t in ("expenditures", "metrics", "grants", "vacancies", "documents",
              "resolutions", "appropriations", "goals", "document_chunks",
              "evaluation_suite"):
        assert f"UPDATE {t} SET department_id = 2" in sql
    assert "UPDATE departments SET active = FALSE" in sql


def test_merge_member_repoints_three_columns_plus_attendance():
    store = _store_with([])
    store.merge_member(dup_id=9, keep_id=1)
    sql = " ".join(_executed(store))
    assert "UPDATE votes SET member_id = 1" in sql
    assert "UPDATE meetings SET president_id = 1" in sql
    assert "UPDATE legislation SET sponsor_id = 1" in sql
    assert "UPDATE meeting_attendance SET member_id = 1" in sql
    assert "UPDATE council_members SET active = FALSE" in sql


def test_merge_vendor_repoints_resolutions():
    store = _store_with([])
    store.merge_vendor(dup_id=4, keep_id=3)
    sql = " ".join(_executed(store))
    assert "UPDATE resolutions SET vendor_id = 3" in sql
    assert "UPDATE vendors SET active = FALSE" in sql
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/storage/test_entity_merge.py -v`
Expected: FAIL — `AttributeError: ... 'merge_department'`

- [ ] **Step 3: Implement the merges**

```python
_DEPT_FK_TABLES = [
    "expenditures", "metrics", "grants", "vacancies", "documents", "resolutions",
    "appropriations", "goals", "document_chunks", "evaluation_suite",
]

def _merge_simple(self, cur, dim_table, alias_table, alias_fk, fk_targets, dup_id, keep_id):
    """fk_targets: list of (table, column) whose column references dim_table.id."""
    cur.execute(
        f"UPDATE {alias_table} SET {alias_fk} = %s WHERE {alias_fk} = %s", (keep_id, dup_id))
    for tbl, col in fk_targets:
        cur.execute(f"UPDATE {tbl} SET {col} = %s WHERE {col} = %s", (keep_id, dup_id))
    cur.execute(f"UPDATE {dim_table} SET active = FALSE WHERE id = %s", (dup_id,))

def merge_department(self, dup_id, keep_id):
    with self.cursor() as cur:
        self._merge_simple(
            cur, "departments", "department_aliases", "department_id",
            [(t, "department_id") for t in _DEPT_FK_TABLES], dup_id, keep_id)

def merge_member(self, dup_id, keep_id):
    with self.cursor() as cur:
        self._merge_simple(
            cur, "council_members", "council_member_aliases", "member_id",
            [("votes", "member_id"), ("meetings", "president_id"),
             ("legislation", "sponsor_id"), ("meeting_attendance", "member_id")],
            dup_id, keep_id)

def merge_vendor(self, dup_id, keep_id):
    with self.cursor() as cur:
        self._merge_simple(
            cur, "vendors", "vendor_aliases", "vendor_id",
            [("resolutions", "vendor_id")], dup_id, keep_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/storage/test_entity_merge.py -v`
Expected: PASS (all 3)

- [ ] **Step 5: Commit**

```bash
git add src/storage/sql_store.py tests/storage/test_entity_merge.py
git commit -m "feat(schema): merge_department/member/vendor for clerk reconciliation"
```

---

### Task 5: Wire resolvers into the ingest insert paths

**Files:**
- Modify: `src/storage/sql_store.py` (the `insert_*_rows` / `record_document` methods)
- Modify: `src/ingestion/pipeline.py:340` (pass `source_file` to `insert_vacancy_rows`)
- Test: `tests/ingestion/test_typed_write_entities.py`

**Interfaces:**
- Consumes: resolvers from Task 3.
- Produces: each insert path writes the new FK column(s) beside the raw string; `insert_vacancy_rows(rows, source_chunk_id, source_file)` gains `source_file`; `insert_meeting_rows` also writes `meeting_attendance` rows.

- [ ] **Step 1: Write the failing test** (extends the repo's typed-write pattern)

```python
# tests/ingestion/test_typed_write_entities.py
from tests.storage.test_entity_resolvers import _store_with


def test_expenditure_insert_sets_department_id(monkeypatch):
    store = _store_with([])
    monkeypatch.setattr(store, "resolve_department_id", lambda name: 42)
    captured = {}
    def fake_exec(sql, rows):           # executemany shim
        captured["sql"] = sql; captured["rows"] = rows
    monkeypatch.setattr(store, "_executemany", fake_exec, raising=False)
    store.insert_expenditure_rows(
        [{"department": "Bureau of Fire", "line_item": "x"}],
        source_chunk_id="00000000-0000-0000-0000-000000000000", source_file="f.pdf")
    assert "department_id" in captured["sql"]
    assert captured["rows"][0]["department_id"] == 42


def test_vacancy_insert_accepts_source_file(monkeypatch):
    store = _store_with([])
    monkeypatch.setattr(store, "resolve_department_id", lambda name: 7)
    # Should not raise on the new source_file argument.
    import inspect
    assert "source_file" in inspect.signature(store.insert_vacancy_rows).parameters
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/ingestion/test_typed_write_entities.py -v`
Expected: FAIL — `department_id` not in the generated SQL / `source_file` not a parameter.

- [ ] **Step 3: Implement the wiring**

For each `insert_*_rows` method that carries a resolvable string, add the FK column to the column list and value dict. Pattern (shown for expenditures — apply the analogous one-line change to metrics, grants, vacancies, resolutions, appropriations, goals for `department_id`; to `insert_vote_rows` for `member_id`; `insert_meeting_rows` for `president_id`; `insert_legislation_rows` for `sponsor_id` via `resolve_member_id(..., create=False)`; `insert_resolution_rows` for `vendor_id`):

```python
# inside insert_expenditure_rows, before the executemany:
for row in rows:
    row["department_id"] = self.resolve_department_id(row.get("department"))
    row.setdefault("sub_department", None)
    row["source_chunk_id"] = uuid.UUID(source_chunk_id)
    row["source_file"] = source_file
```
and add `department_id` to the `INSERT INTO expenditures (...)` column list and `%(department_id)s` to the VALUES.

For `record_document`, before its upsert:
```python
self.ensure_document_type(document_type)
department_id = self.resolve_department_id(department)
```
add `department_id` to the column list / value dict.

For `insert_vacancy_rows`, change the signature to
`def insert_vacancy_rows(self, rows, source_chunk_id, source_file):`, add
`source_file` to the INSERT column list and set `row["source_file"] = source_file`
per row. Then update the call site `src/ingestion/pipeline.py:340` to
`self.sql_store.insert_vacancy_rows(vacancy_rows, chunks[0].chunk_id, source_file)`.

For `insert_meeting_rows`, after inserting each meeting row and obtaining its `id`,
parse attendance and write the junction (raw TEXT columns are untouched):
```python
def _write_attendance(self, cur, meeting_id, present_names, absent_names, expected_present):
    from src.ingestion.names import normalize_person_name
    seen = 0
    for names, status in ((present_names, "present"), (absent_names, "absent")):
        for raw in (names or "").split(","):
            clean = normalize_person_name(raw)
            if not clean:
                continue
            mid = self.resolve_member_id(clean)
            cur.execute(
                "INSERT INTO meeting_attendance (meeting_id, member_id, status) "
                "VALUES (%s, %s, %s) ON CONFLICT (meeting_id, member_id) DO NOTHING",
                (meeting_id, mid, status))
            if status == "present":
                seen += 1
    if expected_present is not None and seen != expected_present:
        self._flag(cur, "validate", "attendance_count_mismatch",
                   f"meeting {meeting_id}: parsed {seen} present vs stated {expected_present}")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/ingestion/test_typed_write_entities.py tests/ingestion/test_pipeline_typed_write.py -v`
Expected: PASS. (Also run the full `pytest -q` to confirm no existing insert-path test regressed from the signature/column changes.)

- [ ] **Step 5: Commit**

```bash
git add src/storage/sql_store.py src/ingestion/pipeline.py tests/ingestion/test_typed_write_entities.py
git commit -m "feat(schema): populate entity FKs + attendance junction on ingest"
```

---

### Task 6: Backfill script (discovery over existing rows)

**Files:**
- Create: `scripts/backfill_entities_2026_07_28.py`
- Test: `tests/storage/test_backfill_entities.py`

**Interfaces:**
- Consumes: `SQLStore` resolvers (auto-create), the migrated schema (Task 2).
- Produces: `backfill(store) -> dict` with counts; when run as `__main__`, discovers dimensions from existing distinct strings, sets FK columns by alias join, and parses attendance for existing meetings.

- [ ] **Step 1: Write the script**

```python
# scripts/backfill_entities_2026_07_28.py
"""One-shot: populate entity dimensions + FK columns from data already in the
tables. Deterministic, no LLM. Run AFTER sql/migrate_2026_07_28_schema_
relationships.sql. Idempotent — safe to re-run; converges as aliases are added.

Usage:  DATABASE_URL=<scratch-or-prod> python -m scripts.backfill_entities_2026_07_28
"""
from __future__ import annotations

from src.storage.sql_store import SQLStore

_DEPT_TABLES = [
    "expenditures", "metrics", "grants", "vacancies", "documents", "resolutions",
    "appropriations", "goals", "document_chunks", "evaluation_suite",
]


def _distinct(cur, table, col):
    cur.execute(
        f"SELECT DISTINCT {col} AS v FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
    return [r["v"] for r in cur.fetchall()]


def backfill(store: SQLStore) -> dict:
    counts = {"departments": 0, "members": 0, "vendors": 0, "attendance": 0}
    with store.cursor() as cur:
        # 1. discover dimensions (auto-create) from every raw string in use
        for table in _DEPT_TABLES:
            for name in _distinct(cur, table, "department"):
                if store.resolve_department_id(name):
                    counts["departments"] += 1
        for name in _distinct(cur, "votes", "council_member") \
                + _distinct(cur, "meetings", "president"):
            if store.resolve_member_id(name):
                counts["members"] += 1
        for name in _distinct(cur, "resolutions", "vendor"):
            if store.resolve_vendor_id(name):
                counts["vendors"] += 1
        # sponsors are match-only (committees must not become members)
        for name in _distinct(cur, "legislation", "sponsor"):
            store.resolve_member_id(name, create=False)

        # 2. set FK columns by alias join (idempotent)
        for table in _DEPT_TABLES:
            cur.execute(
                f"UPDATE {table} t SET department_id = da.department_id "
                f"FROM department_aliases da "
                f"WHERE LOWER(TRIM(t.department)) = LOWER(da.alias) AND t.department_id IS NULL")
        cur.execute(
            "UPDATE votes v SET member_id = a.member_id FROM council_member_aliases a "
            "WHERE LOWER(TRIM(v.council_member)) = LOWER(a.alias) AND v.member_id IS NULL")
        cur.execute(
            "UPDATE meetings m SET president_id = a.member_id FROM council_member_aliases a "
            "WHERE LOWER(TRIM(m.president)) = LOWER(a.alias) AND m.president_id IS NULL")
        cur.execute(
            "UPDATE legislation l SET sponsor_id = a.member_id FROM council_member_aliases a "
            "WHERE LOWER(TRIM(l.sponsor)) = LOWER(a.alias) AND l.sponsor_id IS NULL")
        cur.execute(
            "UPDATE resolutions r SET vendor_id = va.vendor_id FROM vendor_aliases va "
            "WHERE LOWER(TRIM(r.vendor)) = LOWER(va.alias) AND r.vendor_id IS NULL")

        # 3. attendance for existing meetings (raw TEXT stays as audit trail)
        cur.execute("SELECT id, members_present, members_present_names, "
                    "members_absent_names FROM meetings")
        for m in cur.fetchall():
            store._write_attendance(cur, m["id"], m["members_present_names"],
                                    m["members_absent_names"], m["members_present"])
            counts["attendance"] += 1
    return counts


if __name__ == "__main__":
    store = SQLStore()
    store.connect()
    try:
        print(backfill(store))
        store._conn.commit()
    finally:
        store.close()
```

- [ ] **Step 2: Write the failing integration test** (scratch DB; skipped without it)

```python
# tests/storage/test_backfill_entities.py
import os
import pytest
from src.storage.sql_store import SQLStore
from scripts.backfill_entities_2026_07_28 import backfill

pytestmark = pytest.mark.skipif(
    not os.getenv("SCRATCH_DATABASE_URL"), reason="SCRATCH_DATABASE_URL not set")


def test_backfill_discovers_and_links(scratch_store):
    with scratch_store.cursor() as cur:
        cur.execute("INSERT INTO expenditures (department, line_item) VALUES "
                    "('Bureau of Fire', 'a'), ('Bureau of Fire ', 'b')")
        cur.execute("INSERT INTO resolutions (resolution_number, vendor) VALUES "
                    "('21-2026', 'Lamar Advertising, LLC')")
    counts = backfill(scratch_store)
    with scratch_store.cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM departments WHERE canonical_key='fire'")
        assert cur.fetchone()["c"] == 1                     # both variants -> one dept
        cur.execute("SELECT COUNT(*) c FROM expenditures WHERE department_id IS NOT NULL")
        assert cur.fetchone()["c"] == 2                     # both rows linked
        cur.execute("SELECT COUNT(*) c FROM vendors")
        assert cur.fetchone()["c"] == 1
    assert counts["departments"] >= 1
```

Add a `scratch_store` fixture to `tests/storage/conftest.py` (create if absent) that connects `SQLStore` to `SCRATCH_DATABASE_URL`, applies `schema.sql` + the migration, and truncates the fact tables between tests.

- [ ] **Step 3: Run to verify fail, then pass**

Run: `SCRATCH_DATABASE_URL=postgresql://localhost/kb_scratch pytest tests/storage/test_backfill_entities.py -v`
Expected: FAIL before the script exists / fixture is wired; PASS after. Skipped (green) when `SCRATCH_DATABASE_URL` is unset.

- [ ] **Step 4: Commit**

```bash
git add scripts/backfill_entities_2026_07_28.py tests/storage/test_backfill_entities.py tests/storage/conftest.py
git commit -m "feat(schema): discovery backfill for entity dimensions + attendance"
```

---

### Task 7: Harrisburg alias seed + end-to-end verification

**Files:**
- Create: `sql/seed_harrisburg_department_aliases.sql`
- Create: `docs/superpowers/plans/2026-07-28-schema-relationships-VERIFICATION.md`

**Interfaces:**
- Consumes: everything above.
- Produces: an opt-in, Harrisburg-only alias seed; a runbook the human follows to apply against the real DB.

- [ ] **Step 1: Write the Harrisburg alias seed** (replays the dashboard's curated merges)

```sql
-- sql/seed_harrisburg_department_aliases.sql
-- HARRISBURG-ONLY. Do NOT run for any other city. Replays the known-good merges
-- already curated in dashboard/aggregator._DEPT_ALIASES so Harrisburg's dimension
-- doesn't fragment on variants the generic rules can't catch. Run AFTER the
-- backfill (so the canonical departments exist). Idempotent.
-- Each pair: (raw alias seen in data)  ->  (canonical_key of the target dept).
INSERT INTO department_aliases (department_id, alias)
SELECT d.id, v.alias
FROM departments d
JOIN (VALUES
    ('planning',                'Planning Bureau'),
    ('city council',            'Harrisburg City Council'),
    ('city council',            'City of Harrisburg City Council'),
    ('budget & finance',        'Department of Finance'),
    ('budget & finance',        'Departnment of Finance'),
    ('parks & recreation',      'Park Maintenance'),
    ('parks & recreation',      'Parks Maintenance'),
    ('building & housing development',
                                'Department of Economic Development & Building and Housing'),
    ('codes',                   'Codes Health Department'),
    ('police',                  'Buraeu of Police'),
    ('communications',          'Bureau of Coummunicaitons')
) AS v(target_key, alias) ON d.canonical_key = v.target_key
ON CONFLICT (alias) DO NOTHING;
```

- [ ] **Step 2: Write the verification runbook**

Create `docs/superpowers/plans/2026-07-28-schema-relationships-VERIFICATION.md` containing the exact human-run sequence:
1. **Backup** the production DB (Supabase dashboard → database backup) before anything.
2. Point `SCRATCH_DATABASE_URL` at a **Supabase branch** (clone of prod) and run, in order: apply `sql/migrate_2026_07_28_schema_relationships.sql`; `python -m scripts.backfill_entities_2026_07_28`; apply `sql/seed_harrisburg_department_aliases.sql`; re-run the backfill (to pick up the new aliases).
3. Run the verification queries and record results:
   - `SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_type='FOREIGN KEY';` (expect ≥ ~30, was 1)
   - Per table: `SELECT count(*) FILTER (WHERE department_id IS NOT NULL)::float/count(*) FROM <t>;` (coverage)
   - Loop check (one query): minutes → `meeting_actions` → `resolutions` → `votes` → `council_members`.
   - `SELECT count(*) FROM resolutions r JOIN vendors v ON r.vendor_id=v.id WHERE v.canonical_key = vendor_key('Lamar Advertising');` — the Lamar lease resolutions share one vendor.
   - Attendance: per meeting, `count(*) FILTER (status='present')` equals `members_present` (or is in `review_flags`).
   - `SELECT * FROM review_flags WHERE source_file IN ('schema-migration','entity-resolver') AND NOT resolved;` — review unmapped/near-dup/mismatch.
4. Open the Supabase schema diagram on the branch — confirm the six hubs (`documents`, `document_chunks`, `departments`, `council_members`, `meetings`, `resolutions`) with fact tables radiating off them.
5. Only after the branch looks right: apply the same three files to production, then re-run the backfill on production.
6. (Optional, later) After an orphan sweep, `VALIDATE CONSTRAINT` each `NOT VALID` FK to promote it to fully verified.

- [ ] **Step 3: Verify no hardcoded names leaked into the generic migration**

Run: `grep -nE "Bureau of|Department of|Office of" sql/migrate_2026_07_28_schema_relationships.sql sql/schema.sql && echo "FOUND (investigate)" || echo "clean — generic migration has no city names"`
Expected: `clean` (the only file with literal names is `sql/seed_harrisburg_department_aliases.sql`, which is intentional and Harrisburg-only).

- [ ] **Step 4: Commit**

```bash
git add sql/seed_harrisburg_department_aliases.sql docs/superpowers/plans/2026-07-28-schema-relationships-VERIFICATION.md
git commit -m "feat(schema): Harrisburg alias seed + production verification runbook"
```

---

## Self-review notes

- **Spec coverage:** dimensions+resolvers (Tasks 1,3), department_id ×10 tables (Task 2), provenance FKs + NOT VALID (Task 2), votes/meeting_actions legislative links (Task 2), council_members + vendors + sponsor match-only (Tasks 2,3,5), meeting_attendance + validator (Tasks 2,5), registry FKs + auto-register (Tasks 2,3), near-dup flag 0.6 (Task 3), merges (Task 4), backfill discovery (Task 6), Harrisburg alias file (Task 7), vacancies.source_file (Tasks 2,5), fresh-bootstrap parity (Task 2). Deliberate non-links (llm_usage, review_flags) are correctly NOT added.
- **No production writes** occur in any task; DB-touching tests gate on `SCRATCH_DATABASE_URL` and skip cleanly without it, so `pytest -q` stays green in CI with no database.
- **Type consistency:** resolver names (`resolve_department_id`/`resolve_member_id`/`resolve_vendor_id`), alias-FK map (`_ALIAS_FK`), and merge column lists match across Tasks 3–6.
