# Task 7 Report: Resolution Schema, SQL Tables, Store Methods, Registry Entry

## What Was Implemented

### 1. `src/ingestion/schemas/resolution.py` (created)
Three Pydantic models:
- `ResolutionRow` — resolution_number, title, amount (Optional[float]), vendor, department, adopted_date (Optional[str] YYYY-MM-DD), status, source_text, confidence
- `VoteRow` — resolution_number, council_member, vote ("yes"|"no"|"abstain"), source_text, confidence
- `ResolutionExtraction` — resolutions: list[ResolutionRow], votes: list[VoteRow], both with default_factory=list

### 2. `sql/schema.sql` (modified — additive only)
Added to end of file:
- `CREATE TABLE IF NOT EXISTS resolutions` (id, resolution_number, title, amount DECIMAL(15,2), vendor, department, adopted_date DATE, status, source_chunk_id UUID, source_file, ingested_at)
- `CREATE TABLE IF NOT EXISTS votes` (id, resolution_number, council_member, vote, source_chunk_id UUID, source_file, ingested_at)
- Three supporting indexes: idx_resolutions_number, idx_resolutions_vendor, idx_votes_resolution

### 3. `src/storage/sql_store.py` (modified — additive)
Added two methods following `insert_grant_rows` pattern exactly:
- `insert_resolution_rows(rows, source_chunk_id, source_file)` — positional %s params, `with self.cursor() as cur:` loop
- `insert_vote_rows(rows, source_chunk_id, source_file)` — same pattern, 5-column insert

### 4. `src/ingestion/registry.py` (modified)
- Added import: `from src.ingestion.schemas.resolution import ResolutionExtraction`
- Defined `_RESOLUTION = DocumentType(...)` with content_vocab, sql_targets=["resolutions","votes"], graph_targets=["Resolution","Vendor","CouncilMember"], chunking=ChunkingHints(keep_together=["whereas","resolved"]), extraction_schema=ResolutionExtraction
- Called `register(_RESOLUTION)` at module level AFTER function definitions
- Recomputed `KNOWN_TYPE_NAMES = list(_REGISTRY.keys())` AFTER the register call

### 5. `pytest.ini` (modified)
Registered the `integration` marker to silence PytestUnknownMarkWarning.

## Unit Test Results

**RED phase** (before implementation):
```
ERROR: ModuleNotFoundError: No module named 'src.ingestion.schemas.resolution'
```

**GREEN phase** (after implementation):
```
tests/ingestion/test_resolution_schema.py::test_resolution_registered_with_keep_together PASSED
tests/ingestion/test_resolution_schema.py::test_resolution_extraction_parses PASSED
2 passed in 0.02s
```

## Integration Test Status

**Written:** `tests/storage/test_resolution_store.py` — 3 tests covering:
- `test_insert_resolution_rows` — inserts a row, verifies via execute_query, cleans up
- `test_insert_vote_rows` — inserts 2 vote rows, verifies ordering and content
- `test_insert_resolution_rows_null_optional_fields` — verifies amount=None and adopted_date=None work

**Collection:** Collects cleanly with no import errors (confirmed via `--collect-only`).

**Run result:** 3 SKIPPED — Postgres unreachable in this worktree (no .env / no DB). The module-scoped fixture calls `pytest.skip()` on connection failure, so tests skip rather than fail. This is the expected behavior per the brief.

## Full Unit Suite Result

```
pytest -q -m "not integration"
52 passed, 3 deselected, 2 warnings in 3.01s
```
All 52 pre-existing unit tests still pass. The 3 deselected are the new integration tests.

## KNOWN_TYPE_NAMES Confirmation

`register(_RESOLUTION)` is called at module level AFTER the `register()` function is defined. `KNOWN_TYPE_NAMES = list(_REGISTRY.keys())` is placed AFTER that call. As a result:
- `KNOWN_TYPE_NAMES` = `["quarterly_report", "resolution"]`
- `test_every_registered_type_is_wellformed` iterates `all_document_types()` and checks each name against `KNOWN_TYPE_NAMES` — passes for both types.

## Files Changed

| File | Action |
|------|--------|
| `src/ingestion/schemas/resolution.py` | Created |
| `sql/schema.sql` | Modified (additive) |
| `src/storage/sql_store.py` | Modified (additive) |
| `src/ingestion/registry.py` | Modified (additive) |
| `tests/ingestion/test_resolution_schema.py` | Created |
| `tests/storage/__init__.py` | Created |
| `tests/storage/test_resolution_store.py` | Created |
| `pytest.ini` | Modified (registered integration marker) |

## Commit

`512ee84` — `feat(ingestion): add resolution type, schema, SQL tables, and store methods`

## Self-Review

- Followed `insert_grant_rows` pattern exactly (positional %s, `with self.cursor() as cur:`, per-row loop).
- Dollar amounts stored as plain float/DECIMAL — no currency wrapping.
- Dates stored as YYYY-MM-DD strings or None — no coercion beyond `or None` for empty strings.
- Did not modify any existing model, table, or method — purely additive.
- `vocab=None` / no-hints paths unaffected: `ChunkingHints()` default for quarterly_report unchanged.
- No new LLM code introduced.

## Concerns

None. The integration tests follow the same fixture-skip pattern that would be standard for this project when a Postgres database is available. When the worktree is given a `.env` with `DATABASE_URL` and the schema is applied, all 3 integration tests should pass without modification.

---

## Fix Report (post-Task-7 review fixes)

### Fix 1: Parameterize test SELECTs (`tests/storage/test_resolution_store.py`)

Three f-string SELECTs passed to `execute_query` were replaced with parameterized cursor calls:

| Location | Before | After |
|----------|--------|-------|
| `test_insert_resolution_rows` (line ~44) | `store.execute_query(f"SELECT * FROM resolutions WHERE source_file = '{source_file}'")`| `with store.cursor() as cur: cur.execute("SELECT * FROM resolutions WHERE source_file = %s", (source_file,)); result = [dict(r) for r in cur.fetchall()]` |
| `test_insert_vote_rows` (line ~74) | `store.execute_query(f"SELECT * FROM votes WHERE source_file = '{source_file}' ORDER BY council_member")` | `with store.cursor() as cur: cur.execute("SELECT * FROM votes WHERE source_file = %s ORDER BY council_member", (source_file,)); result = [dict(r) for r in cur.fetchall()]` |
| `test_insert_resolution_rows_null_optional_fields` (line ~105) | `store.execute_query(f"SELECT * FROM resolutions WHERE source_file = '{source_file}'")`| `with store.cursor() as cur: cur.execute("SELECT * FROM resolutions WHERE source_file = %s", (source_file,)); result = [dict(r) for r in cur.fetchall()]` |

All test assertions remain identical.

### Fix 2: Clear resolutions/votes on re-ingest (`src/storage/sql_store.py` — `delete_structured_rows`)

Added `"resolutions"` and `"votes"` to the simple delete-by-source_file loop in `delete_structured_rows`. The vacancies special-case (JOIN through document_chunks) is unchanged.

Before:
```python
for table in ["expenditures", "metrics", "grants"]:
```
After:
```python
for table in ["expenditures", "metrics", "grants", "resolutions", "votes"]:
```

### Commands Run + Output

**Collection check:**
```
$ python3 -m pytest tests/storage/test_resolution_store.py --collect-only
============================= test session starts ==============================
collected 3 items
  <Function test_insert_resolution_rows>
  <Function test_insert_vote_rows>
  <Function test_insert_resolution_rows_null_optional_fields>
========================== 3 tests collected in 0.04s ==========================
```

**Full unit suite:**
```
$ python3 -m pytest -q -m "not integration"
....................................................  [100%]
52 passed, 3 deselected, 2 warnings in 2.95s
```

No local Postgres available; 3 integration tests deselected (expected).
