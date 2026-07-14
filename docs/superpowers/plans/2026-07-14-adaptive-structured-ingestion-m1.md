# Adaptive Structured Ingestion — M1: Triage + Proposal Queue — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a document is classified `unclassified`, an agent detects any valuable
structured data, reconciles it against the live schema (map into existing tables vs.
propose a new one), and writes a *proposal* to a review queue surfaced read-only in the
dashboard. No schema is mutated and no structured rows are written in M1 — this milestone
delivers detection + drafted mappings + sample rows for human review.

**Architecture:** A new `triage` step invoked from the pipeline's unclassified branch,
using Haiku against a live `information_schema`-derived schema summary; results validated
against a `TriageResult` Pydantic schema and persisted to a new `type_proposals` table;
a read-only dashboard endpoint + panel lists pending proposals.

**Tech Stack:** Python, Pydantic v2, psycopg2 (JSONB), Anthropic Haiku via the existing
`TrackedAnthropic` wrapper, Flask dashboard.

## Global Constraints

- **Accuracy #1:** M1 writes NOTHING to structured tables and mutates NO schema. It only
  writes to `type_proposals`. Apply/DDL is M3.
- **Limited funds:** triage runs Haiku (`cfg.profile_model`) and ONLY when `doc_type is
  None` (unclassified) AND `cfg.enable_triage` is true. One call per unclassified doc.
  Every call is logged via `TrackedAnthropic` (call_site `ingestion.triage`).
- **Reuse patterns:** mirror `parse_quarterly_response` (tolerant JSON parse), the
  `_SeqClient` fake-LLM test pattern, and `insert_review_flag`/`get_unresolved_review_flags`
  store-accessor style.
- Follow existing code conventions (RealDictCursor dict rows, `with self.cursor()`).

---

## File Structure

- Create: `sql/migrate_2026_07_14_type_proposals.sql` — the queue table.
- Modify: `sql/schema.sql` — add `type_proposals` (canonical schema).
- Create: `src/ingestion/schemas/triage.py` — `TriageResult` + nested models.
- Create: `src/ingestion/triage.py` — schema-summary helper + `run_triage`.
- Modify: `src/storage/sql_store.py` — `insert_type_proposal`, `get_pending_type_proposals`.
- Modify: `src/ingestion/pipeline.py` — call triage in the unclassified branch.
- Modify: `src/config.py` — `enable_triage` flag.
- Modify: `app.py` — `GET /proposals` endpoint.
- Modify: `templates/redesign.html` — read-only proposals panel.
- Create tests: `tests/ingestion/test_triage.py`, `tests/storage/test_type_proposals.py`
  (integration), `tests/ingestion/test_triage_wiring.py`, `tests/dashboard/test_proposals_route.py`.

---

### Task 1: `type_proposals` table

**Files:**
- Create: `sql/migrate_2026_07_14_type_proposals.sql`
- Modify: `sql/schema.sql` (add table near `review_flags`)

**Interfaces:**
- Produces: table `type_proposals(id, source_file, proposed_type, status, payload jsonb,
  created_at, reviewed_at, reviewer_note)`, consumed by Task 5.

- [ ] **Step 1: Write the migration**

```sql
-- sql/migrate_2026_07_14_type_proposals.sql
-- Queue of agent-proposed structured-data types/mappings awaiting human review.
-- Idempotent.
CREATE TABLE IF NOT EXISTS type_proposals (
    id            SERIAL PRIMARY KEY,
    source_file   VARCHAR(255) NOT NULL,
    proposed_type VARCHAR(100),
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    payload       JSONB NOT NULL,
    created_at    TIMESTAMP DEFAULT NOW(),
    reviewed_at   TIMESTAMP,
    reviewer_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_type_proposals_status ON type_proposals(status);
```

- [ ] **Step 2: Mirror it into `sql/schema.sql`** (append the identical `CREATE TABLE` +
  index after the `review_flags` block so a fresh DB has it).

- [ ] **Step 3: Apply to live DB** (operator step)

Run: `python3 -c "from src.config import get_settings; from src.storage.sql_store import SQLStore; s=SQLStore(get_settings()); s.connect();\nimport pathlib;\nc=s.cursor().__enter__();\nc.execute(pathlib.Path('sql/migrate_2026_07_14_type_proposals.sql').read_text()); print('ok')"`
Expected: `ok` and the table exists (`\d type_proposals`).

- [ ] **Step 4: Commit**

```bash
git add sql/migrate_2026_07_14_type_proposals.sql sql/schema.sql
git commit -m "feat(triage): type_proposals queue table"
```

---

### Task 2: `TriageResult` schema

**Files:**
- Create: `src/ingestion/schemas/triage.py`
- Test: `tests/ingestion/test_triage.py`

**Interfaces:**
- Produces: `TriageResult`, `RecordTypeProposal`, `ProposedColumn` (imported by Task 4 & 6).

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_triage.py
import json
from src.ingestion.schemas.triage import TriageResult


def test_triage_result_parses_fit_and_new():
    payload = json.dumps({
        "has_structured_data": True,
        "proposed_type_name": "boards_commissions",
        "record_types": [
            {"name": "board_member", "target": "new", "match_confidence": 0.9,
             "proposed_columns": [{"name": "board", "type": "VARCHAR(120)"},
                                  {"name": "member_name", "type": "VARCHAR(120)"}],
             "sample_rows": [{"board": "Audit Committee", "member_name": "Ed Jaroch"}]},
            {"name": "appointment_ref", "target": "existing", "existing_table": "resolutions",
             "column_mapping": {"resolution": "resolution_number"}, "match_confidence": 0.7,
             "sample_rows": [{"resolution": "31-2023"}]},
        ],
    })
    r = TriageResult.model_validate_json(payload)
    assert r.has_structured_data is True
    assert r.record_types[0].target == "new"
    assert r.record_types[0].proposed_columns[0].name == "board"
    assert r.record_types[1].existing_table == "resolutions"
```

- [ ] **Step 2: Run it, verify it fails** (`ModuleNotFoundError: src.ingestion.schemas.triage`).

- [ ] **Step 3: Implement the schema**

```python
# src/ingestion/schemas/triage.py
"""Contract for the ingest-side triage agent: does an unclassified doc contain
structured data worth storing, and where should each record-type go — an existing
table (with a column mapping) or a proposed new table?"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class ProposedColumn(BaseModel):
    name: str
    type: str = Field(description="one of TEXT, VARCHAR(n), INTEGER, DECIMAL(15,2), DATE, BOOLEAN")
    description: str = ""


class RecordTypeProposal(BaseModel):
    name: str
    description: str = ""
    target: str = Field(description='"existing" or "new"')
    existing_table: Optional[str] = None
    column_mapping: Optional[dict[str, str]] = None      # doc_field -> existing column
    proposed_columns: Optional[list[ProposedColumn]] = None
    match_confidence: float = 0.0
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


class TriageResult(BaseModel):
    has_structured_data: bool = False
    proposed_type_name: str = ""
    record_types: list[RecordTypeProposal] = Field(default_factory=list)
```

- [ ] **Step 4: Run the test, verify it passes.**

- [ ] **Step 5: Commit** (`git commit -m "feat(triage): TriageResult schema"`).

---

### Task 3: Live schema-summary helper

**Files:**
- Create: `src/ingestion/triage.py` (helper only in this task)
- Test: `tests/ingestion/test_triage.py`

**Interfaces:**
- Consumes: a store exposing `cursor()` (RealDictCursor).
- Produces: `schema_summary(store) -> str` (used by Task 4 & the triage prompt).

- [ ] **Step 1: Write the failing test** (fake cursor returning information_schema rows)

```python
# add to tests/ingestion/test_triage.py
from src.ingestion.triage import schema_summary


class _FakeCur:
    def __init__(self, rows): self._rows = rows
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeStore:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCur(self._rows)


def test_schema_summary_groups_columns_by_table():
    rows = [
        {"table_name": "grants", "column_name": "department", "data_type": "character varying"},
        {"table_name": "grants", "column_name": "amount", "data_type": "numeric"},
        {"table_name": "vacancies", "column_name": "position_title", "data_type": "character varying"},
    ]
    out = schema_summary(_FakeStore(rows))
    assert "grants(department, amount)" in out
    assert "vacancies(position_title)" in out
```

- [ ] **Step 2: Run it, verify it fails** (`cannot import name 'schema_summary'`).

- [ ] **Step 3: Implement the helper**

```python
# src/ingestion/triage.py
"""Ingest-side triage: detect structured data in an unclassified document and reconcile
it against the LIVE schema (map into existing tables vs. propose a new one). M1 only
proposes — it never writes structured rows or mutates schema."""
from __future__ import annotations
import json
import logging
from typing import Optional

from src.config import Settings, get_settings
from src.llm.client import TrackedAnthropic
from src.ingestion.schemas.triage import TriageResult

logger = logging.getLogger(__name__)

# Structured tables the triage agent may reconcile against.
_STRUCTURED_TABLES = (
    "expenditures", "metrics", "grants", "vacancies", "goals", "projects",
    "resolutions", "votes", "meetings", "meeting_actions", "legislation", "appropriations",
)


def schema_summary(store) -> str:
    """One line per table: `table(col1, col2, ...)`, from live information_schema."""
    with store.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) "
            "ORDER BY table_name, ordinal_position",
            (list(_STRUCTURED_TABLES),),
        )
        rows = cur.fetchall()
    cols: dict[str, list[str]] = {}
    for r in rows:
        cols.setdefault(r["table_name"], []).append(r["column_name"])
    return "\n".join(f"{t}({', '.join(c)})" for t, c in cols.items())
```

- [ ] **Step 4: Run the test, verify it passes.**

- [ ] **Step 5: Commit** (`git commit -m "feat(triage): live schema-summary helper"`).

---

### Task 4: The triage agent (`run_triage`)

**Files:**
- Modify: `src/ingestion/triage.py`
- Test: `tests/ingestion/test_triage.py`

**Interfaces:**
- Consumes: doc text, `schema_summary` string, an LLM client (inject for tests).
- Produces: `run_triage(text, schema_text, llm, attempts=2) -> TriageResult`. Returns a
  `has_structured_data=False` result on parse failure (never raises).

- [ ] **Step 1: Write the failing tests** (fake sequential client; reuse the pattern from
  `tests/extraction/test_quarterly_extraction.py`)

```python
# add to tests/ingestion/test_triage.py
from src.ingestion.triage import run_triage, build_triage_prompt


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _SeqClient:
    def __init__(self, payloads): self._p = list(payloads); self._i = 0
    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k):
            o = self._o; p = o._p[min(o._i, len(o._p) - 1)]; o._i += 1
            return _FakeMsg(p)
    @property
    def messages(self): return _SeqClient._M(self)


def test_run_triage_returns_validated_result():
    good = json.dumps({"has_structured_data": True, "proposed_type_name": "boards",
        "record_types": [{"name": "board_member", "target": "new", "match_confidence": 0.9,
            "proposed_columns": [{"name": "board", "type": "VARCHAR(120)"}],
            "sample_rows": [{"board": "Audit Committee"}]}]})
    r = run_triage("some text", "grants(department, amount)", _SeqClient([good]))
    assert r.has_structured_data and r.record_types[0].name == "board_member"


def test_run_triage_safe_on_bad_json():
    r = run_triage("x", "grants(department)", _SeqClient(["not json", "still not"]))
    assert r.has_structured_data is False and r.record_types == []


def test_build_triage_prompt_includes_schema_and_rules():
    p = build_triage_prompt("BODY TEXT", "grants(department, amount)")
    assert "grants(department, amount)" in p
    assert "existing" in p and "BODY TEXT" in p
```

- [ ] **Step 2: Run them, verify they fail** (`cannot import name 'run_triage'`).

- [ ] **Step 3: Implement `build_triage_prompt` + `run_triage`**

```python
# append to src/ingestion/triage.py

def build_triage_prompt(text: str, schema_text: str) -> str:
    schema_json = json.dumps(TriageResult.model_json_schema())
    return (
        "You are a data-architecture triage agent for a City of Harrisburg knowledge base.\n"
        "Decide whether this document contains structured, record-like data worth storing "
        "in SQL (rosters, tables, per-item records) — as opposed to purely narrative prose.\n\n"
        "If it does, identify each RECORD TYPE and reconcile it against the EXISTING schema "
        "below. For each record type choose a target:\n"
        "  - \"existing\": the SAME KIND of record already has a table — give existing_table "
        "and a column_mapping (doc field -> existing column). Only choose this when it is "
        "genuinely the same kind of record, not merely column-similar. When unsure, prefer "
        "\"new\" or a low match_confidence.\n"
        "  - \"new\": no existing table fits — propose columns (types limited to TEXT, "
        "VARCHAR(n), INTEGER, DECIMAL(15,2), DATE, BOOLEAN).\n"
        "Include up to 5 verbatim sample_rows per record type so a human can judge quality.\n\n"
        f"EXISTING TABLES:\n{schema_text}\n\n"
        f"Return ONLY JSON matching this schema:\n{schema_json}\n\n"
        f"Document:\n---\n{text}\n---"
    )


def run_triage(text: str, schema_text: str, llm, attempts: int = 2) -> TriageResult:
    """Run one triage pass. Returns an empty (has_structured_data=False) result rather
    than raising, so a triage failure never blocks ingestion."""
    prompt = build_triage_prompt(text, schema_text)
    for attempt in range(attempts):
        try:
            msg = llm.messages.create(
                model=_triage_model(), max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            return TriageResult.model_validate_json(raw)
        except Exception as e:
            logger.warning("triage attempt %d/%d failed: %s", attempt + 1, attempts, e)
    return TriageResult()


_CFG: Optional[Settings] = None


def _triage_model() -> str:
    global _CFG
    if _CFG is None:
        _CFG = get_settings()
    return _CFG.profile_model
```

- [ ] **Step 4: Run the tests, verify they pass.**

- [ ] **Step 5: Commit** (`git commit -m "feat(triage): run_triage agent + prompt"`).

---

### Task 5: Store accessors for proposals

**Files:**
- Modify: `src/storage/sql_store.py`
- Test: `tests/storage/test_type_proposals.py` (marked `integration`)

**Interfaces:**
- Produces: `insert_type_proposal(source_file, proposed_type, payload: dict) -> None`,
  `get_pending_type_proposals() -> list[dict]` (consumed by Task 6 & 7).

- [ ] **Step 1: Write the failing integration test**

```python
# tests/storage/test_type_proposals.py
import pytest
from src.config import get_settings
from src.storage.sql_store import SQLStore

pytestmark = pytest.mark.integration


def test_insert_and_fetch_pending_proposal():
    store = SQLStore(get_settings()); store.connect()
    store.insert_type_proposal("triage_test.pdf", "boards",
                               {"has_structured_data": True, "record_types": []})
    pending = store.get_pending_type_proposals()
    assert any(p["source_file"] == "triage_test.pdf" for p in pending)
    with store.cursor() as cur:
        cur.execute("DELETE FROM type_proposals WHERE source_file = 'triage_test.pdf'")
    store.close()
```

- [ ] **Step 2: Run it, verify it fails** (`AttributeError: insert_type_proposal`).

- [ ] **Step 3: Implement the accessors** (near `insert_review_flag`)

```python
# src/storage/sql_store.py  (psycopg2.extras is already imported)
    def insert_type_proposal(self, source_file: str, proposed_type: str, payload: dict) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO type_proposals (source_file, proposed_type, payload) "
                "VALUES (%s, %s, %s)",
                (source_file, proposed_type, psycopg2.extras.Json(payload)),
            )

    def get_pending_type_proposals(self) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, source_file, proposed_type, payload, created_at "
                "FROM type_proposals WHERE status = 'pending' ORDER BY created_at DESC"
            )
            return [dict(r) for r in cur.fetchall()]
```

- [ ] **Step 4: Run the test** (`pytest -m integration tests/storage/test_type_proposals.py`),
  verify it passes.

- [ ] **Step 5: Commit** (`git commit -m "feat(triage): type_proposals store accessors"`).

---

### Task 6: Wire triage into the pipeline

**Files:**
- Modify: `src/config.py` (add `enable_triage: bool = True`)
- Modify: `src/ingestion/pipeline.py`
- Test: `tests/ingestion/test_triage_wiring.py`

**Interfaces:**
- Consumes: `run_triage`, `schema_summary` (Task 3/4), `insert_type_proposal` (Task 5),
  `profile` (has `.document_type`, `.proposed_type`), `cfg.enable_triage`.
- Produces: side effect — a `type_proposals` row when an unclassified doc has structured data.

- [ ] **Step 1: Add the config flag**

```python
# src/config.py (in Settings)
    enable_triage: bool = True   # run the structured-data triage agent on unclassified docs
```

- [ ] **Step 2: Write the failing test** (fakes; mirrors `test_quarterly_wiring.py`)

```python
# tests/ingestion/test_triage_wiring.py
from src.ingestion import pipeline as P
from src.config import get_settings
from src.models import Chunk, ChunkMetadata


def _chunk():
    m = ChunkMetadata(source_file="b.pdf", department="", document_type="unclassified",
                      quarter="", year=2026, section="s", content_type="narrative",
                      page_number=1, parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text="Audit Committee members: Ed Jaroch ...", metadata=m)


class _Vec:
    def upsert_chunks(self, chunks): pass


class _Store:
    def __init__(self): self.proposals = []
    def cursor(self): raise AssertionError("schema_summary should be monkeypatched")
    def insert_type_proposal(self, sf, pt, payload): self.proposals.append((sf, pt, payload))


class _Profile:
    document_type = "unclassified"; proposed_type = "boards_commissions"; confidence = 0.3
    department = ""; period = ""


def test_unclassified_doc_with_structured_data_creates_proposal(monkeypatch):
    from src.ingestion.schemas.triage import TriageResult, RecordTypeProposal
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.cfg = get_settings(); pipe.cfg.enable_triage = True
    pipe.vector_store = _Vec(); pipe.sql_store = _Store()
    monkeypatch.setattr(P, "schema_summary", lambda store: "grants(department)")
    monkeypatch.setattr(P, "run_triage", lambda text, schema, llm=None:
        TriageResult(has_structured_data=True, proposed_type_name="boards_commissions",
                     record_types=[RecordTypeProposal(name="board_member", target="new",
                                                      match_confidence=0.9)]))
    pipe.triage_llm = object()
    pipe._store_chunks([_chunk()], "b.pdf", None, quarantined=True, profile=_Profile())
    assert pipe.sql_store.proposals and pipe.sql_store.proposals[0][1] == "boards_commissions"


def test_unclassified_without_structured_data_no_proposal(monkeypatch):
    from src.ingestion.schemas.triage import TriageResult
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.cfg = get_settings(); pipe.cfg.enable_triage = True
    pipe.vector_store = _Vec(); pipe.sql_store = _Store()
    monkeypatch.setattr(P, "schema_summary", lambda store: "grants(department)")
    monkeypatch.setattr(P, "run_triage", lambda text, schema, llm=None: TriageResult())
    pipe.triage_llm = object()
    pipe._store_chunks([_chunk()], "b.pdf", None, quarantined=True, profile=_Profile())
    assert pipe.sql_store.proposals == []
```

- [ ] **Step 3: Run the tests, verify they fail** (no triage in `_store_chunks`).

- [ ] **Step 4: Implement the wiring.** At the top of `pipeline.py`, add imports:

```python
from src.ingestion.triage import run_triage, schema_summary
```

In `IngestionPipeline.__init__`, add a triage LLM client (Haiku call-site) alongside the
other clients:

```python
        self.triage_llm = TrackedAnthropic(self.cfg, call_site="ingestion.triage")
```

In `_store_chunks`, replace the early quarantine return:

```python
        if quarantined or doc_type is None:
            return
```

with a triage hook for the unclassified case:

```python
        if quarantined or doc_type is None:
            # Unclassified (no known type) → triage for structured data worth proposing.
            # Only here (not for parse/garble quarantine of a KNOWN type) and only if enabled.
            if doc_type is None and self.cfg.enable_triage and profile is not None:
                try:
                    text = "\n\n".join(c.text for c in chunks)
                    result = run_triage(text, schema_summary(self.sql_store), self.triage_llm)
                    if result.has_structured_data and result.record_types:
                        self.sql_store.insert_type_proposal(
                            source_file,
                            result.proposed_type_name or (profile.proposed_type or "unknown"),
                            result.model_dump(),
                        )
                        logger.info("  → %s: triage proposed type %r (%d record types)",
                                    source_file, result.proposed_type_name, len(result.record_types))
                except Exception as e:
                    logger.warning("triage failed for %s (non-fatal): %s", source_file, e)
            return
```

- [ ] **Step 5: Run the tests, verify they pass.**

- [ ] **Step 6: Run the full suite** (`python3 -m pytest -q -m "not integration"`), expect
  green (no regressions).

- [ ] **Step 7: Commit** (`git commit -m "feat(triage): run triage on unclassified docs, persist proposals"`).

---

### Task 7: Read-only proposals endpoint + dashboard panel

**Files:**
- Modify: `app.py` (add `GET /proposals`)
- Modify: `templates/redesign.html` (add a read-only panel)
- Test: `tests/dashboard/test_proposals_route.py`

**Interfaces:**
- Consumes: `store.get_pending_type_proposals()` (Task 5).
- Produces: `GET /proposals` → JSON list; a dashboard panel rendering it.

- [ ] **Step 1: Write the failing test** (Flask test client with a fake store)

```python
# tests/dashboard/test_proposals_route.py
import app as A


def test_proposals_route_returns_pending(monkeypatch):
    class _Store:
        def get_pending_type_proposals(self):
            return [{"id": 1, "source_file": "b.pdf", "proposed_type": "boards",
                     "payload": {"record_types": [{"name": "board_member"}]},
                     "created_at": "2026-07-14T00:00:00"}]
    monkeypatch.setattr(A, "_sql_store", _Store())
    client = A.app.test_client()
    resp = client.get("/proposals")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data[0]["proposed_type"] == "boards"
```

- [ ] **Step 2: Run it, verify it fails** (404 / no route).

- [ ] **Step 3: Implement the endpoint** (near the other read routes in `app.py`)

```python
@app.route("/proposals", methods=["GET"])
def proposals():
    try:
        return jsonify(_sql_store.get_pending_type_proposals())
    except Exception as e:
        logger.exception("proposals route failed")
        return jsonify({"error": "could not load proposals"}), 500
```

- [ ] **Step 4: Run the test, verify it passes.**

- [ ] **Step 5: Add the read-only panel to `templates/redesign.html`.** Near the other
  dashboard panels, add a container and a fetch/render block (display-only; approve/reject
  is M3). Escape all interpolated values via the existing `esc()` helper.

```html
<section id="proposals-panel" style="margin-top:24px">
  <h2 style="font-size:15px;font-weight:700">Proposed data types (pending review)</h2>
  <div id="proposals-list" style="font-size:13px;color:#1c1b19"></div>
</section>
<script>
(async function(){
  try{
    const r = await fetch('/proposals'); if(!r.ok) return;
    const items = await r.json(); const el = document.getElementById('proposals-list');
    if(!items.length){ el.textContent = 'No pending proposals.'; return; }
    el.innerHTML = items.map(p => {
      const rts = (p.payload && p.payload.record_types || []).map(rt =>
        `<li>${esc(rt.name||'')} → ${esc(rt.target||'')}` +
        (rt.existing_table? ' ('+esc(rt.existing_table)+')' : '') + `</li>`).join('');
      return `<div class="card" style="padding:12px;margin:8px 0">
        <div style="font-weight:600">${esc(p.proposed_type||'?')}</div>
        <div style="color:#8a867d">${esc(p.source_file||'')}</div>
        <ul style="margin:6px 0 0 16px">${rts}</ul></div>`;
    }).join('');
  }catch(e){ /* panel is best-effort */ }
})();
</script>
```

- [ ] **Step 6: Manual verify** — run the app locally, load the dashboard, confirm the
  panel renders pending proposals (or "No pending proposals.").

- [ ] **Step 7: Commit** (`git commit -m "feat(triage): read-only proposals dashboard panel + /proposals"`).

---

## Self-Review

- **Spec coverage:** M1 = triage + reconciliation-aware proposal + queue + read-only
  review. Apply/DDL (M3), data-driven registry (M2), schema-aware query (M4) are
  explicitly out of M1. ✓
- **Accuracy:** no structured-table writes, no schema mutation in M1; triage is
  non-fatal (wrapped in try/except) and gated to unclassified + `enable_triage`. ✓
- **Type consistency:** `run_triage`/`schema_summary` names match between Task 3/4, the
  pipeline import (Task 6), and tests. `insert_type_proposal`/`get_pending_type_proposals`
  match between Task 5, 6, 7. ✓
- **Cost:** one Haiku call per unclassified doc only; logged via `TrackedAnthropic`. ✓

## Execution Handoff

After review, choose execution mode (subagent-driven vs inline) — I'll follow up with
the M2 plan (data-driven registry) once M1 is implemented and verified.
