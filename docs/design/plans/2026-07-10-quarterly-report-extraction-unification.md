# Quarterly-Report Extraction Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract expenditures, metrics, grants, vacancies, goals, and projects from every quarterly report via one schema-driven pass over all chunks, removing the format-dependent `routes_to_sql()` gate and keyword filters.

**Architecture:** Replace the special-cased `quarterly_report` branch in `pipeline.py` with a single batched extraction (`SQLExtractor.extract_quarterly`) against the `QuarterlyReportExtraction` Pydantic schema. Precision lives in schema field descriptions (declarative), not per-table prompt code. A new `projects` table captures the "Special Projects" sections. A backfill script re-extracts existing chunks (no re-parse/re-embed).

**Tech Stack:** Python 3.14, Pydantic v2, psycopg2, Anthropic (Sonnet via `TrackedAnthropic`), pytest.

## Global Constraints

- No LLM re-extraction runs (backfill) without explicit user approval — limited funds.
- Extraction rows MUST carry a verbatim `source_text`; keep only `high`/`medium` confidence.
- Do NOT touch the graph path (`routes_to_graph`, `graph_extractor.*`) or other document types (resolution/minutes/legislation/budget).
- `goals.user_status` / `user_status_at` are human-set; never extracted, never wiped by backfill.
- Execution should run in an isolated git worktree (a concurrent session commits in this repo).
- DB dependent tests use `@pytest.mark.integration` (deselected by default via `-m "not integration"`).

---

### Task 1: Extend the extraction schema + registry targets

**Files:**
- Modify: `src/ingestion/schemas/quarterly_report.py`
- Modify: `src/ingestion/registry.py:12-23`
- Test: `tests/ingestion/test_quarterly_schema.py` (create)

**Interfaces:**
- Produces: `QuarterlyReportExtraction` with lists `expenditures, metrics, grants, vacancies, goals, projects`; `VacancyRow.count: Optional[int]`; new `GoalRow`, `ProjectRow`. `get_document_type("quarterly_report").sql_targets == ["expenditures","metrics","grants","vacancies","goals","projects"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_quarterly_schema.py
import json
from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
from src.ingestion.registry import get_document_type


def test_schema_has_all_six_targets():
    fields = QuarterlyReportExtraction.model_fields
    assert set(fields) == {"expenditures", "metrics", "grants", "vacancies", "goals", "projects"}


def test_vacancy_row_has_count_and_project_row_present():
    schema = json.dumps(QuarterlyReportExtraction.model_json_schema())
    assert "count" in schema and "project_name" in schema and "funding_source" in schema


def test_registry_targets_updated():
    dt = get_document_type("quarterly_report")
    assert dt.sql_targets == ["expenditures", "metrics", "grants", "vacancies", "goals", "projects"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/ingestion/test_quarterly_schema.py -v`
Expected: FAIL — schema lacks `goals`/`projects`; registry targets differ.

- [ ] **Step 3: Rewrite the schema file**

Replace the body of `src/ingestion/schemas/quarterly_report.py` with (descriptions carry the precision rules that used to live in per-table prompts):

```python
"""Extraction contract for quarterly reports — one schema, all structured targets.

Field descriptions carry the precision rules (e.g. grants = external awards, not
budget lines) so accuracy is declarative and department-agnostic, not baked into
bespoke per-table prompt code."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ExpenditureRow(BaseModel):
    account_number: str = Field("", description="account number from a structured budget/Munis table; not a narrative dollar mention")
    line_item: str = ""
    sub_department: str = ""
    revised_budget: Optional[float] = None
    ytd_expended: Optional[float] = None
    source_text: str
    confidence: str


class MetricRow(BaseModel):
    metric_name: str
    metric_value: float = Field(..., description="explicitly stated count/total/rate; never inferred or calculated")
    metric_unit: str = "count"
    source_text: str
    confidence: str


class GrantRow(BaseModel):
    grant_name: str = Field(..., description="EXTERNAL award to the City (federal/state/county/foundation); NOT a budget line, appropriation, spending figure, or salary")
    grant_number: str = ""
    amount: Optional[float] = Field(None, description="TOTAL award amount to the City, not a spending line")
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = ""
    source_text: str
    confidence: str


class VacancyRow(BaseModel):
    position_title: str = Field(..., description="role name, singular, WITHOUT the count")
    status: str = Field(..., description='exactly "open" or "filled"')
    count: Optional[int] = Field(None, description="number of open positions for this title, e.g. the parenthesized number in 'Patrol Officer- (25)', else null")
    source_text: str
    confidence: str


class GoalRow(BaseModel):
    goal_title: str = Field(..., description="the goal's heading/name, however the section is labeled (Annual Goals, Objectives, Priorities, or an unlabeled list)")
    description: str = ""
    target: str = Field("", description="only if a quantified aim is stated, else ''")
    status: str = Field("", description="only if progress is stated, else ''")
    source_text: str
    confidence: str


class ProjectRow(BaseModel):
    project_name: str = Field(..., description="a department initiative / special project named in the report, e.g. 'Porch Lights & Ring Doorbells', 'Saturation Details', 'Funding a Forensic Investigator Position'")
    description: str = ""
    status: str = Field("", description="only if stated, else ''")
    funding_source: str = Field("", description="grant/fund name only if stated, else ''")
    source_text: str
    confidence: str


class QuarterlyReportExtraction(BaseModel):
    expenditures: list[ExpenditureRow] = Field(default_factory=list)
    metrics: list[MetricRow] = Field(default_factory=list)
    grants: list[GrantRow] = Field(default_factory=list)
    vacancies: list[VacancyRow] = Field(default_factory=list)
    goals: list[GoalRow] = Field(default_factory=list)
    projects: list[ProjectRow] = Field(default_factory=list)
```

- [ ] **Step 4: Update the registry sql_targets**

In `src/ingestion/registry.py`, change the `_QUARTERLY_REPORT` definition's `sql_targets` line to:

```python
    sql_targets=["expenditures", "metrics", "grants", "vacancies", "goals", "projects"],
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/ingestion/test_quarterly_schema.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/schemas/quarterly_report.py src/ingestion/registry.py tests/ingestion/test_quarterly_schema.py
git commit -m "feat(ingest): extend quarterly schema with goals+projects, declarative precision"
```

---

### Task 2: `projects` table + store methods

**Files:**
- Modify: `sql/schema.sql` (after the `vacancies` table block)
- Create: `sql/migrate_2026_07_10_projects.sql`
- Modify: `src/storage/sql_store.py` (add `insert_project_rows`; add `"projects"` to `delete_structured_rows`)
- Test: `tests/storage/test_projects_store.py` (create)

**Interfaces:**
- Produces: `SQLStore.insert_project_rows(rows: list[dict], source_chunk_id: str, source_file: str) -> None`. `delete_structured_rows(source_file)` also clears `projects`.

- [ ] **Step 1: Write the failing test (integration — needs live DB)**

```python
# tests/storage/test_projects_store.py
import pytest
from src.config import get_settings
from src.storage.sql_store import SQLStore


@pytest.mark.integration
def test_insert_and_delete_project_rows():
    s = SQLStore(get_settings()); s.connect()
    cid = "00000000-0000-0000-0000-000000000001"
    try:
        s.insert_project_rows(
            [{"department": "ZTest Dept", "project_name": "Porch Lights",
              "description": "camera + lighting pilot", "status": "ongoing",
              "funding_source": "LLES-2023", "quarter": "Q1", "year": 2025}],
            cid, "z-projtest.pdf")
        rows = s.execute_query("SELECT project_name, funding_source FROM projects WHERE source_file='z-projtest.pdf'")
        assert rows and rows[0]["project_name"] == "Porch Lights" and rows[0]["funding_source"] == "LLES-2023"
        s.delete_structured_rows("z-projtest.pdf")
        rows2 = s.execute_query("SELECT 1 FROM projects WHERE source_file='z-projtest.pdf'")
        assert rows2 == []
    finally:
        with s.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE source_file = 'z-projtest.pdf'")
        s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/storage/test_projects_store.py -v -m integration`
Expected: FAIL — `projects` table / `insert_project_rows` do not exist.

- [ ] **Step 3: Add the table to schema.sql**

In `sql/schema.sql`, immediately after the `CREATE TABLE IF NOT EXISTS vacancies (...);` block, add:

```sql
-- Special-project / initiative tracking from quarterly reports
CREATE TABLE IF NOT EXISTS projects (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    project_name    VARCHAR(300),
    description     TEXT,
    status          VARCHAR(50),
    funding_source  VARCHAR(200),
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    source_file     VARCHAR(255),
    ingested_at     TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_dept ON projects(department);
```

- [ ] **Step 4: Create the migration**

```sql
-- sql/migrate_2026_07_10_projects.sql
-- Projects table migration (2026-07-10). Idempotent; safe to re-run.
CREATE TABLE IF NOT EXISTS projects (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    project_name    VARCHAR(300),
    description     TEXT,
    status          VARCHAR(50),
    funding_source  VARCHAR(200),
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    source_file     VARCHAR(255),
    ingested_at     TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_dept ON projects(department);
```

- [ ] **Step 5: Apply the migration to the DB**

Run:
```bash
python3 -c "
url=[l.split('=',1)[1].strip() for l in open('.env') if l.startswith('DATABASE_URL=')][0]
import psycopg2; c=psycopg2.connect(url); c.autocommit=True
c.cursor().execute(open('sql/migrate_2026_07_10_projects.sql').read()); print('applied')"
```
Expected: `applied`

- [ ] **Step 6: Add `insert_project_rows` and update `delete_structured_rows`**

In `src/storage/sql_store.py`, add this method next to `insert_goal_rows`:

```python
    def insert_project_rows(self, rows: list[dict], source_chunk_id: str, source_file: str) -> None:
        sql = """
            INSERT INTO projects
              (department, project_name, description, status, funding_source,
               quarter, year, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("department"), r.get("project_name"), r.get("description"),
                    r.get("status"), r.get("funding_source"), r.get("quarter"),
                    r.get("year"), source_chunk_id, source_file,
                ))
```

In `delete_structured_rows`, add `"projects"` to the table list:

```python
            for table in ["expenditures", "metrics", "grants", "resolutions", "votes",
                          "meetings", "meeting_actions", "legislation", "appropriations",
                          "goals", "projects"]:
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python3 -m pytest tests/storage/test_projects_store.py -v -m integration`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add sql/schema.sql sql/migrate_2026_07_10_projects.sql src/storage/sql_store.py tests/storage/test_projects_store.py
git commit -m "feat(ingest): projects table + insert_project_rows + delete cleanup"
```

---

### Task 3: `extract_quarterly` — batched schema extraction

**Files:**
- Modify: `src/extraction/sql_extractor.py` (add `extract_quarterly` + `_schema_extract_batch`)
- Test: `tests/extraction/test_quarterly_extraction.py` (create)

**Interfaces:**
- Consumes: `QuarterlyReportExtraction` (Task 1).
- Produces: `SQLExtractor.extract_quarterly(chunks: list[Chunk], department: str = "", quarter: str = "", year: Optional[int] = None) -> dict[str, list[dict]]` — keys `expenditures, metrics, grants, vacancies, goals, projects`; every row tagged with `department`/`quarter`/`year`; `source_text`/`confidence` stripped; only `high`/`medium` kept. Never raises → `{}` on failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/extraction/test_quarterly_extraction.py
import json
from src.extraction.sql_extractor import SQLExtractor
from src.models import Chunk, ChunkMetadata


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _SeqClient:
    """Returns a different payload per call (one per batch)."""
    def __init__(self, payloads): self._p = list(payloads); self._i = 0

    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k):
            o = self._o; p = o._p[min(o._i, len(o._p) - 1)]; o._i += 1
            return _FakeMsg(p)

    @property
    def messages(self): return _SeqClient._M(self)


def _chunk(text, i=0):
    m = ChunkMetadata(source_file="r.pdf", department="Bureau of Police",
                      document_type="quarterly_report", quarter="Q1", year=2025,
                      section="s", content_type="narrative", page_number=1,
                      parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=i, total_chunks_in_doc=2)
    return Chunk(text=text, metadata=m)


def test_extract_quarterly_merges_batches_tags_and_filters():
    batch1 = json.dumps({"vacancies": [
        {"position_title": "Patrol Officer", "status": "open", "count": 25,
         "source_text": "Patrol Officer- (25)", "confidence": "high"}],
        "projects": [{"project_name": "Porch Lights", "description": "pilot",
                      "status": "", "funding_source": "", "source_text": "Porch Lights", "confidence": "high"}]})
    batch2 = json.dumps({"goals": [
        {"goal_title": "Reduce response time", "description": "", "target": "", "status": "",
         "source_text": "Goal: reduce response time", "confidence": "high"}],
        "metrics": [{"metric_name": "cases", "metric_value": 52, "metric_unit": "count",
                     "source_text": "52 Cases", "confidence": "low"}]})
    from src.config import Settings
    cfg = Settings(); cfg.extraction_batch_size = 1  # isolated settings; force two batches
    ext = SQLExtractor(settings=cfg, llm=_SeqClient([batch1, batch2]))
    out = ext.extract_quarterly([_chunk("a", 0), _chunk("b", 1)],
                                department="Bureau of Police", quarter="Q1", year=2025)
    # merged across batches
    assert out["vacancies"][0]["position_title"] == "Patrol Officer"
    assert out["vacancies"][0]["count"] == 25
    assert out["projects"][0]["project_name"] == "Porch Lights"
    assert out["goals"][0]["goal_title"] == "Reduce response time"
    # low-confidence metric dropped
    assert "metrics" not in out or out["metrics"] == []
    # tagged with period, source_text stripped
    v = out["vacancies"][0]
    assert v["department"] == "Bureau of Police" and v["quarter"] == "Q1" and v["year"] == 2025
    assert "source_text" not in v and "confidence" not in v


def test_extract_quarterly_empty_and_bad_json_safe():
    assert SQLExtractor(llm=_SeqClient(["{}"])).extract_quarterly([]) == {}
    ext = SQLExtractor(llm=_SeqClient(["not json"]))
    assert ext.extract_quarterly([_chunk("x")]) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/extraction/test_quarterly_extraction.py -v`
Expected: FAIL — `extract_quarterly` not defined.

- [ ] **Step 3: Implement `extract_quarterly` + helper**

In `src/extraction/sql_extractor.py`, add near `extract_for_type` (keep imports at top of method local to avoid load-order issues):

```python
    def _schema_extract_batch(self, chunks, schema_cls) -> dict[str, list[dict]]:
        """One LLM call over a chunk batch against schema_cls. Returns raw dict of
        lists filtered to high/medium confidence. Never raises → {} on failure."""
        try:
            text = "\n\n---\n\n".join(c.text for c in chunks)
            schema_json = json.dumps(schema_cls.model_json_schema())
            prompt = (
                "You are a precise data extractor for City of Harrisburg quarterly reports.\n"
                "Extract EVERYTHING matching this JSON schema, wherever it appears in the text "
                "(sections are labeled differently by each department — do not rely on headings).\n"
                f"{schema_json}\n\n"
                "Rules: include a verbatim 'source_text' for every row; set 'confidence' "
                "high|medium|low and omit low-confidence rows; dollar amounts as plain numbers; "
                "dates YYYY-MM-DD or null. Return ONLY the JSON object.\n\nText:\n---\n" + text + "\n---"
            )
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model, max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            data = schema_cls.model_validate_json(raw).model_dump()
            return {k: [r for r in v if r.get("confidence") in ("high", "medium")]
                    for k, v in data.items() if v}
        except Exception as e:
            logger.warning("quarterly batch extraction failed: %s", e)
            return {}

    def extract_quarterly(self, chunks, department: str = "", quarter: str = "",
                          year: Optional[int] = None) -> dict[str, list[dict[str, Any]]]:
        """Unified schema-driven extraction for a whole quarterly report. Batches ALL
        chunks (no routes_to_sql gate, no keyword filter), merges rows across batches,
        tags each with department/quarter/year, strips extraction-only fields."""
        from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
        if not chunks:
            return {}
        merged: dict[str, list] = {}
        batch_size = self.cfg.extraction_batch_size
        for i in range(0, len(chunks), batch_size):
            part = self._schema_extract_batch(chunks[i:i + batch_size], QuarterlyReportExtraction)
            for key, rows in part.items():
                merged.setdefault(key, []).extend(rows)
        for rows in merged.values():
            for r in rows:
                r.pop("source_text", None)
                r.pop("confidence", None)
                r["department"] = department
                r["quarter"] = quarter
                r["year"] = year
        return {k: v for k, v in merged.items() if v}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/extraction/test_quarterly_extraction.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/extraction/sql_extractor.py tests/extraction/test_quarterly_extraction.py
git commit -m "feat(ingest): extract_quarterly — batched whole-report schema extraction"
```

---

### Task 4: Rewrite the pipeline quarterly branch

**Files:**
- Modify: `src/ingestion/pipeline.py:305-351` (the `if doc_type.name == "quarterly_report":` block)
- Test: `tests/ingestion/test_quarterly_wiring.py` (create)

**Interfaces:**
- Consumes: `extract_quarterly` (Task 3), `insert_project_rows` (Task 2), existing `insert_*` methods.
- Produces: quarterly branch routes all six tables from one `extract_quarterly` call; graph path unchanged.

- [ ] **Step 1: Write the failing test (fakes, no DB)**

```python
# tests/ingestion/test_quarterly_wiring.py
from src.ingestion import pipeline as P
from src.config import get_settings
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata


def _chunk(i=0):
    m = ChunkMetadata(source_file="r.pdf", department="Bureau of Police",
                      document_type="quarterly_report", quarter="Q1", year=2025,
                      section="s", content_type="narrative", page_number=1,
                      parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=i, total_chunks_in_doc=1)
    return Chunk(text="Patrol Officer- (25)", metadata=m)


class _FakeStore:
    def __init__(self): self.calls = {}
    def _rec(self, name, rows): self.calls[name] = rows
    def insert_expenditure_rows(self, r, c, f): self._rec("expenditures", r)
    def insert_metric_rows(self, r, c, f): self._rec("metrics", r)
    def insert_grant_rows(self, r, c, f): self._rec("grants", r)
    def insert_vacancy_rows(self, r, c): self._rec("vacancies", r)
    def insert_goal_rows(self, r, c, f): self._rec("goals", r)
    def insert_project_rows(self, r, c, f): self._rec("projects", r)


class _FakeVector:
    def upsert_chunks(self, chunks): pass


class _FakeExtractor:
    def extract_quarterly(self, chunks, department="", quarter="", year=None):
        return {"vacancies": [{"position_title": "Patrol Officer", "status": "open",
                               "count": 25, "department": department, "quarter": quarter, "year": year}],
                "projects": [{"project_name": "Porch Lights", "department": department}]}


class _Profile:
    department = "Bureau of Police"; period = "Q1 2025"


def test_quarterly_branch_routes_all_targets():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.cfg = get_settings()
    pipe.vector_store = _FakeVector()
    pipe.sql_store = _FakeStore()
    pipe.sql_extractor = _FakeExtractor()
    pipe.graph_extractor = None  # graph path guarded below; no graph chunks here
    dt = get_document_type("quarterly_report")
    pipe._store_chunks([_chunk()], "r.pdf", dt, quarantined=False, profile=_Profile())
    assert pipe.sql_store.calls["vacancies"][0]["count"] == 25
    assert pipe.sql_store.calls["projects"][0]["project_name"] == "Porch Lights"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/ingestion/test_quarterly_wiring.py -v`
Expected: FAIL — branch still calls `extract_chunks_batched`/`extract_grants`/etc. (AttributeError on `_FakeExtractor`).

- [ ] **Step 3: Replace the quarterly branch**

In `src/ingestion/pipeline.py`, replace the entire block from `if doc_type.name == "quarterly_report":` through its `return` (currently lines 305-351) with:

```python
        # quarterly_report: ONE schema-driven pass over all chunks (no routes_to_sql
        # gate, no keyword filters) → all six structured targets. See
        # docs/superpowers/specs/2026-07-10-quarterly-report-extraction-unification-design.md
        if doc_type.name == "quarterly_report":
            q, y = metadata._split_period(profile.period) if profile else ("", None)
            dept = (profile.department if profile else "") or ""
            data = self.sql_extractor.extract_quarterly(
                chunks, department=dept, quarter=q or "", year=y)
            problems = validation.validate_extraction("quarterly_report", data or {}, profile)
            if problems:
                logger.warning("  → %s failed validation, withholding structured write: %s",
                               source_file, "; ".join(problems))
                self.sql_store.insert_review_flag(source_file, "validate", "; ".join(problems), "")
            elif data:
                cid = str(chunks[0].chunk_id)
                if data.get("expenditures"):
                    self.sql_store.insert_expenditure_rows(data["expenditures"], cid, source_file)
                if data.get("metrics"):
                    self.sql_store.insert_metric_rows(data["metrics"], cid, source_file)
                if data.get("grants"):
                    self.sql_store.insert_grant_rows(data["grants"], cid, source_file)
                if data.get("vacancies"):
                    self.sql_store.insert_vacancy_rows(data["vacancies"], cid)
                if data.get("goals"):
                    self.sql_store.insert_goal_rows(data["goals"], cid, source_file)
                if data.get("projects"):
                    self.sql_store.insert_project_rows(data["projects"], cid, source_file)

            graph_chunks = [c for c in chunks if c.routes_to_graph()]
            if graph_chunks:
                try:
                    graph_data = self.graph_extractor.extract_chunks_batched(graph_chunks)
                    self._write_graph_data(graph_data, source_file, chunks[0].metadata)
                    chunk_ids = [str(c.chunk_id) for c in graph_chunks]
                    self.graph_store.link_chunks_to_entities(chunk_ids, graph_data)
                except Exception as e:
                    logger.warning("Graph store write failed (vector+SQL still complete): %s", e)
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ingestion/test_quarterly_wiring.py -v`
Expected: PASS. (The test provides no graph chunks, so the graph branch is skipped and `graph_extractor=None` is never dereferenced.)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/pipeline.py tests/ingestion/test_quarterly_wiring.py
git commit -m "feat(ingest): route quarterly_report through unified extract_quarterly"
```

---

### Task 5: Remove superseded dead code

**Files:**
- Modify: `src/extraction/sql_extractor.py` (delete `extract_batch`, `extract_chunks_batched`, `_parse_extraction_response`, `extract_grants`, `extract_vacancies`, `extract_goals`, and now-unused `_sanitize_rows`/`_VARCHAR_LIMITS`/`_EXTRACTION_ONLY_FIELDS` if only those referenced them)
- Delete: `tests/extraction/test_vacancy_extraction.py`
- Modify: `src/ingestion/pipeline.py` — remove the now-unused `_write_sql_data` method (lines ~422-445) if no other caller remains

**Interfaces:**
- Produces: no new interface; removes dead methods. `extract_for_type`, `extract_meeting`, `extract_quarterly` remain.

- [ ] **Step 1: Confirm no remaining references**

Run:
```bash
grep -rn "extract_grants\|extract_goals\|extract_vacancies\|extract_chunks_batched\|_parse_extraction_response\|_write_sql_data\|_sanitize_rows" src/ tests/ | grep -v "graph_extractor" | grep -v "tests/extraction/test_vacancy_extraction.py"
```
Expected: only definitions inside `src/extraction/sql_extractor.py` and `_write_sql_data` in `pipeline.py` (no live callers). If any live caller appears, STOP and reconcile before deleting.

- [ ] **Step 2: Delete the dead code**

Remove from `src/extraction/sql_extractor.py`: the methods `extract_batch`, `extract_chunks_batched`, `extract_goals`, `extract_grants`, `extract_vacancies`, and the module-level `_parse_extraction_response`, `_sanitize_rows`, `_VARCHAR_LIMITS`, `_EXTRACTION_ONLY_FIELDS`. Keep `extract_for_type`, `extract_meeting`, `extract_quarterly`, `_schema_extract_batch`. Remove `_write_sql_data` from `src/ingestion/pipeline.py`. Delete the file `tests/extraction/test_vacancy_extraction.py`.

- [ ] **Step 3: Run the full suite (non-integration)**

Run: `python3 -m pytest -q -m "not integration"`
Expected: PASS except the one pre-existing unrelated failure `tests/query/test_classifier_prompt.py::test_prompt_warns_grants_has_no_quarter_year`. No import errors, no new failures.

- [ ] **Step 4: Commit**

```bash
git add -u src/extraction/sql_extractor.py src/ingestion/pipeline.py
git rm tests/extraction/test_vacancy_extraction.py
git commit -m "refactor(ingest): remove keyword/gate extractors superseded by extract_quarterly"
```

---

### Task 6: Backfill script with `user_status` safeguard

**Files:**
- Create: `scripts/reextract_quarterly.py`
- Test: `tests/ingestion/test_userstatus_safeguard.py` (create)

**Interfaces:**
- Produces: `merge_user_status(existing: list[dict], fresh: list[dict]) -> tuple[list[dict], list[dict]]` — returns `(fresh_with_status_reapplied, unmatched)`; matches on `(department, year, quarter, goal_title)`; only carries `user_status`/`user_status_at` where set. Script `main(write: bool)` runs the backfill over all quarterly reports.

- [ ] **Step 1: Write the failing test for the pure helper**

```python
# tests/ingestion/test_userstatus_safeguard.py
from scripts.reextract_quarterly import merge_user_status


def test_reapplies_matching_status_and_reports_unmatched():
    existing = [
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "Cut response time",
         "user_status": "on_track", "user_status_at": "2026-01-01"},
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "Old title drifted",
         "user_status": "at_risk", "user_status_at": "2026-01-02"},
    ]
    fresh = [
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "Cut response time"},
        {"department": "Police", "year": 2025, "quarter": "Q1", "goal_title": "A brand new goal"},
    ]
    merged, unmatched = merge_user_status(existing, fresh)
    by_title = {r["goal_title"]: r for r in merged}
    assert by_title["Cut response time"]["user_status"] == "on_track"
    assert "user_status" not in by_title["A brand new goal"]
    assert [u["goal_title"] for u in unmatched] == ["Old title drifted"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/ingestion/test_userstatus_safeguard.py -v`
Expected: FAIL — module/function does not exist.

- [ ] **Step 3: Write the script**

```python
# scripts/reextract_quarterly.py
#!/usr/bin/env python3
"""Backfill: re-extract ALL quarterly reports from EXISTING chunks (no re-parse/
re-embed) through the unified extract_quarterly path. Preserves human-set goal
user_status. Operator-run (live Supabase + ANTHROPIC_API_KEY).

Usage:
  python3 scripts/reextract_quarterly.py           # dry-run (LLM calls, no writes)
  python3 scripts/reextract_quarterly.py --write    # re-extract + write
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.extraction.sql_extractor import SQLExtractor
from src.storage.sql_store import SQLStore


def merge_user_status(existing: list[dict], fresh: list[dict]):
    """Re-apply user_status/user_status_at from existing goal rows onto freshly
    extracted ones, matching (department, year, quarter, goal_title). Returns
    (fresh_with_status, unmatched_existing)."""
    def key(r): return (r.get("department"), r.get("year"), r.get("quarter"), r.get("goal_title"))
    set_status = {key(r): r for r in existing if r.get("user_status")}
    for r in fresh:
        prior = set_status.pop(key(r), None)
        if prior:
            r["user_status"] = prior["user_status"]
            r["user_status_at"] = prior.get("user_status_at")
    return fresh, list(set_status.values())


_TABLE_INSERT = [
    ("expenditures", "insert_expenditure_rows", True),
    ("metrics", "insert_metric_rows", True),
    ("grants", "insert_grant_rows", True),
    ("vacancies", "insert_vacancy_rows", False),   # no source_file arg
    ("goals", "insert_goal_rows", True),
    ("projects", "insert_project_rows", True),
]


def main(write: bool):
    cfg = get_settings()
    store = SQLStore(cfg); store.connect()
    ext = SQLExtractor(cfg)
    with store.cursor() as cur:
        cur.execute("SELECT DISTINCT source_file, department, quarter, year "
                    "FROM document_chunks WHERE document_type='quarterly_report' "
                    "ORDER BY department, year, quarter")
        reports = [dict(r) for r in cur.fetchall()]
    print(f"Quarterly reports: {len(reports)} | mode: {'WRITE' if write else 'DRY-RUN'}\n")

    for rep in reports:
        sf, dept, q, y = rep["source_file"], rep["department"], rep["quarter"], rep["year"]
        with store.cursor() as cur:
            cur.execute("SELECT chunk_id, text FROM document_chunks WHERE source_file=%s ORDER BY chunk_id", (sf,))
            chunk_rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT department, year, quarter, goal_title, user_status, user_status_at "
                        "FROM goals WHERE source_file=%s", (sf,))
            prior_goals = [dict(r) for r in cur.fetchall()]

        class _C:  # minimal chunk shim: extract_quarterly only reads .text
            def __init__(self, t): self.text = t
        data = ext.extract_quarterly([_C(c["text"]) for c in chunk_rows],
                                     department=dept, quarter=q or "", year=y)
        counts = {k: len(v) for k, v in data.items()}
        print(f"{sf}\n   {dept} {q} {y} -> {counts}")

        if data.get("goals"):
            data["goals"], unmatched = merge_user_status(prior_goals, data["goals"])
            for u in unmatched:
                print(f"   [user_status] UNMATCHED (kept in DB not possible after delete): "
                      f"{u['goal_title']!r} status={u['user_status']}")

        if not write:
            print()
            continue

        cid = str(chunk_rows[0]["chunk_id"])
        store.delete_structured_rows(sf)
        for key, method, has_file in _TABLE_INSERT:
            rows = data.get(key)
            if not rows:
                continue
            if has_file:
                getattr(store, method)(rows, cid, sf)
            else:
                getattr(store, method)(rows, cid)
        print(f"   wrote {sum(counts.values())} rows\n")
    store.close()


if __name__ == "__main__":
    main(write="--write" in sys.argv)
```

Note: `extract_quarterly` only reads `chunk.text`, so the `_C` shim is sufficient; no full `Chunk` construction needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/ingestion/test_userstatus_safeguard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/reextract_quarterly.py tests/ingestion/test_userstatus_safeguard.py
git commit -m "feat(ingest): quarterly backfill script + user_status safeguard"
```

---

## Backfill execution (AFTER code lands — requires explicit user approval; LLM spend)

Not a code task. Run in this order, stopping at each checkpoint:

1. `python3 scripts/reextract_quarterly.py` (dry-run) on all reports; eyeball counts.
2. **Grant-precision checkpoint:** pick a grant-heavy report; confirm the dry-run's grant rows are real external awards, not budget lines. If regressed, tighten `GrantRow` description (Task 1) and re-run dry-run before proceeding.
3. **Goals-detection checkpoint:** pick a report that labels goals as "Objectives"/"Priorities"; confirm goals now appear.
4. Get user approval, then `python3 scripts/reextract_quarterly.py --write`.
5. Spot-check: police vacancies still 20 rows w/ counts; a previously-empty department (e.g. Bureau of Communications) now has metrics/vacancies.

## Self-Review notes

- **Spec coverage:** unified pass (Tasks 3-4), schema precision descriptions (Task 1), goals fold-in (Tasks 1,3,4), projects table (Tasks 1,2,3,4), backfill + user_status safeguard (Task 6), dead-code removal (Task 5). All spec sections mapped.
- **Correction vs spec:** grant-precision is verified at the **dry-run checkpoint**, not by a unit test — a stubbed LLM can't exercise prompt precision. Spec's testing bullet is refined accordingly here.
- **Type consistency:** `extract_quarterly(chunks, department, quarter, year) -> dict[str,list[dict]]` used identically in Tasks 3, 4, 6; `insert_project_rows(rows, cid, source_file)` defined in Task 2, called in Tasks 4 and 6; `merge_user_status` signature matches its test.
