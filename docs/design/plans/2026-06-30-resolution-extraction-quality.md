# Resolution Extraction Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make resolution extraction reliable by anchoring it to the single resolution the profiler identified (eliminating hallucinated-duplicate/cross-contamination), and normalize council-member names so the graph stops fragmenting one person into many nodes.

**Architecture:** Add an optional `anchor_field` to a `DocumentType`. When set and the profiler found that identifier, `extract_for_type` injects an anchor instruction into the prompt AND applies a deterministic post-extraction guard that collapses the primary table to exactly one row keyed to that identifier. A small `normalize_person_name` helper canonicalizes vote member names before they reach SQL/graph.

**Tech Stack:** Python 3.11+, Pydantic v2, the existing `TrackedAnthropic` LLM client, `pytest`.

## Global Constraints

- The quarterly-report path (`extract_chunks_batched` / `_write_sql_data`) MUST stay unchanged — this is resolution/one-per-file work only.
- `extract_for_type` MUST still never raise (returns `{}` on any failure); the anchor block and guard are best-effort within that contract.
- Anchoring activates ONLY when `doc_type.anchor_field` is set AND `profile.identifying_ids[anchor_field]` is present; otherwise behavior is byte-for-byte unchanged (no profile → no anchoring).
- The "primary table" is the FIRST entry in `doc_type.sql_targets` (for resolution: `resolutions`). Votes (`votes`) are secondary and are never collapsed by the guard.
- Parameterized SQL only; dates ISO; amounts plain numbers — unchanged from current extractor.
- Tests run with `pytest`; live-DB validation is an operator step (no live DB in CI/worktree).

---

### Task 1: `anchor_field` on DocumentType + resolution registry entry

**Files:**
- Modify: `src/models.py` (add field to `DocumentType`)
- Modify: `src/ingestion/registry.py` (resolution entry sets it)
- Test: `tests/ingestion/test_registry.py` (extend)

**Interfaces:**
- Produces: `DocumentType.anchor_field: Optional[str] = None`; `get_document_type("resolution").anchor_field == "resolution_number"`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/ingestion/test_registry.py
def test_resolution_has_anchor_field():
    from src.ingestion.registry import get_document_type
    assert get_document_type("resolution").anchor_field == "resolution_number"

def test_quarterly_report_has_no_anchor_field():
    from src.ingestion.registry import get_document_type
    assert get_document_type("quarterly_report").anchor_field is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_registry.py -k anchor -v`
Expected: FAIL (`AttributeError: 'DocumentType' object has no attribute 'anchor_field'`)

- [ ] **Step 3: Add the field to `DocumentType` in `src/models.py`**

In the `DocumentType` model, add (after `metadata_schema`):

```python
    anchor_field: Optional[str] = None   # identifier that uniquely keys the single primary record in a one-record-per-document type (e.g. "resolution_number")
```

- [ ] **Step 4: Set it on the resolution entry in `src/ingestion/registry.py`**

In the `_RESOLUTION = DocumentType(...)` call, add the kwarg:

```python
    anchor_field="resolution_number",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_registry.py -v`
Expected: PASS (existing + 2 new)

- [ ] **Step 6: Commit**

```bash
git add src/models.py src/ingestion/registry.py tests/ingestion/test_registry.py
git commit -m "feat(ingestion): add anchor_field to DocumentType; resolution anchors on resolution_number"
```

---

### Task 2: `normalize_person_name` helper

**Files:**
- Create: `src/ingestion/names.py`
- Test: `tests/ingestion/test_names.py`

**Interfaces:**
- Produces: `normalize_person_name(name: str | None) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_names.py
from src.ingestion.names import normalize_person_name

def test_trims_and_collapses_whitespace():
    assert normalize_person_name("  john   smith ") == "John Smith"

def test_strips_leading_titles():
    assert normalize_person_name("Councilman Jones") == "Jones"
    assert normalize_person_name("Council Member O'Brien") == "O'Brien"
    assert normalize_person_name("Vice President Smith") == "Smith"
    assert normalize_person_name("Dr. Patel") == "Patel"

def test_case_folds():
    assert normalize_person_name("SMITH") == "Smith"

def test_empty_and_none():
    assert normalize_person_name("") == ""
    assert normalize_person_name(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_names.py -v`
Expected: FAIL (`ModuleNotFoundError: src.ingestion.names`)

- [ ] **Step 3: Implement `src/ingestion/names.py`**

```python
"""Canonicalize person names so graph/SQL don't fragment one person into many."""
from __future__ import annotations

import re

# Leading honorifics/titles to strip (longest/most-specific first), case-insensitive.
_TITLE_RE = re.compile(
    r"^(?:"
    r"council\s*member|councilmember|councilman|councilwoman|council\s*president|"
    r"vice\s+president|president|"
    r"mrs|mr|ms|dr|hon|honorable"
    r")\.?\s+",
    re.IGNORECASE,
)


def normalize_person_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\s+", " ", str(name)).strip()
    # Strip leading titles repeatedly (e.g. "Hon. Council Member X").
    prev = None
    while cleaned and cleaned != prev:
        prev = cleaned
        cleaned = _TITLE_RE.sub("", cleaned).strip()
    return cleaned.title()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_names.py -v`
Expected: PASS (4 tests). Note: `"O'Brien".title()` → `"O'Brien"`; verify your cases match `str.title()` behavior and adjust expectations only if a real name breaks (do not weaken the title-stripping assertions).

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/names.py tests/ingestion/test_names.py
git commit -m "feat(ingestion): add normalize_person_name helper"
```

---

### Task 3: Anchored extraction in `extract_for_type`

**Files:**
- Modify: `src/extraction/sql_extractor.py`
- Test: `tests/extraction/test_anchored_extraction.py`

**Interfaces:**
- Consumes: `DocumentType.anchor_field` (Task 1); `src.models.DocumentProfile` (`.identifying_ids`, `.department`, `.period`).
- Produces: `SQLExtractor.extract_for_type(chunks, doc_type, profile=None)` — new optional `profile`. When `doc_type.anchor_field` is set and `profile.identifying_ids[anchor_field]` exists: (a) the prompt gains an anchor block naming that identifier; (b) the primary table (`doc_type.sql_targets[0]`) is collapsed to exactly one row, preferring the row whose `anchor_field` matches, then forcing that row's `anchor_field` to the profiler value.

- [ ] **Step 1: Write the failing test**

```python
# tests/extraction/test_anchored_extraction.py
import json
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata, DocumentProfile

def _chunk(text):
    m = ChunkMetadata(source_file="r.pdf", department="DEDBH", document_type="resolution",
                      quarter="", year=2026, section="", content_type="legal_authorization",
                      page_number=1, parser_used="vision_llm", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text=text, metadata=m)

class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]
class _FakeClient:
    def __init__(self, payload): self._p = payload; self.last_prompt = None
    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k):
            self._o.last_prompt = k["messages"][0]["content"]
            return _FakeMsg(self._o._p)
    @property
    def messages(self): return _FakeClient._M(self)

# payload mimics the real contamination: a bogus extra resolution alongside the right one
_PAYLOAD = json.dumps({
    "resolutions": [
        {"resolution_number": "7-2026", "amount": 19500.0, "vendor": "Floura Teeter",
         "source_text": "x", "confidence": "high"},
        {"resolution_number": "9-2026", "amount": 3000000.0, "vendor": "US DOT",
         "source_text": "y", "confidence": "high"},
    ],
    "votes": [{"resolution_number": "9-2026", "council_member": "Smith", "vote": "yes",
               "source_text": "z", "confidence": "high"}],
})

def _profile():
    return DocumentProfile(document_type="resolution", department="DEDBH",
                           period="2026-02-10", identifying_ids={"resolution_number": "9-2026"},
                           confidence=0.9)

def test_anchor_block_includes_resolution_number():
    c = _FakeClient(_PAYLOAD)
    SQLExtractor(llm=c).extract_for_type([_chunk("...")], get_document_type("resolution"), profile=_profile())
    assert "9-2026" in c.last_prompt and "SINGLE" in c.last_prompt

def test_guard_collapses_to_single_keyed_row():
    out = SQLExtractor(llm=_FakeClient(_PAYLOAD)).extract_for_type(
        [_chunk("...")], get_document_type("resolution"), profile=_profile())
    assert len(out["resolutions"]) == 1
    assert out["resolutions"][0]["resolution_number"] == "9-2026"
    assert out["resolutions"][0]["amount"] == 3000000.0     # kept the matching row, not the bogus 7-2026
    assert len(out["votes"]) == 1                            # votes not collapsed

def test_no_anchoring_without_profile():
    out = SQLExtractor(llm=_FakeClient(_PAYLOAD)).extract_for_type(
        [_chunk("...")], get_document_type("resolution"), profile=None)
    assert len(out["resolutions"]) == 2                      # unchanged behavior
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/extraction/test_anchored_extraction.py -v`
Expected: FAIL (`extract_for_type()` takes no `profile` kwarg)

- [ ] **Step 3: Edit `extract_for_type` in `src/extraction/sql_extractor.py`**

Change the signature to `def extract_for_type(self, chunks, doc_type, profile=None)`. Compute the anchor before building the prompt, inject the anchor block, and apply the guard after building `result`:

```python
        # --- anchor setup ---
        anchor_field = getattr(doc_type, "anchor_field", None)
        anchor_value = None
        anchor_block = ""
        if anchor_field and profile is not None:
            anchor_value = (profile.identifying_ids or {}).get(anchor_field)
            if anchor_value:
                anchor_block = (
                    f"\nThis document is a SINGLE {doc_type.name}. Its {anchor_field} is "
                    f"\"{anchor_value}\" (department: {profile.department or 'unknown'}, "
                    f"period: {profile.period or 'unknown'}). Extract exactly ONE primary record "
                    f"for THIS document plus its vote record. Do NOT invent additional "
                    f"{doc_type.name}s or split it into multiple records.\n"
                )
```

Insert `+ anchor_block` into the prompt right after the first "You are a precise data extractor…" line. Then, after the existing `result = {...}` dict-comprehension and before `return result`, add the deterministic guard:

```python
            # deterministic anchor guard: collapse the primary table to exactly one
            # row keyed to the profiler's identifier (kills hallucinated duplicates).
            if anchor_value and doc_type.sql_targets:
                primary = doc_type.sql_targets[0]
                rows = result.get(primary) or []
                if rows:
                    match = next(
                        (r for r in rows if str(r.get(anchor_field)) == str(anchor_value)),
                        rows[0],
                    )
                    match[anchor_field] = anchor_value
                    result[primary] = [match]
            return result
```

(The current method ends by returning the comprehension directly — refactor it to assign `result = {...}` then run the guard then `return result`, keeping the existing confidence/sql_targets filter intact.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/extraction/test_anchored_extraction.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing extractor tests (no regression)**

Run: `pytest tests/extraction/ -v`
Expected: PASS (the existing `extract_for_type` tests still pass — `profile` defaults to None, unchanged path)

- [ ] **Step 6: Commit**

```bash
git add src/extraction/sql_extractor.py tests/extraction/test_anchored_extraction.py
git commit -m "feat(extraction): anchored single-record extraction via profile + anchor_field guard"
```

---

### Task 4: Wire profile through the pipeline + normalize vote names

**Files:**
- Modify: `src/ingestion/pipeline.py`
- Test: `tests/ingestion/test_pipeline_typed_write.py`

**Interfaces:**
- Consumes: `extract_for_type(..., profile=...)` (Task 3), `normalize_person_name` (Task 2).
- Produces: `_store_chunks(self, chunks, source_file, doc_type, quarantined, profile=None)` forwards `profile` to `extract_for_type`; `_write_typed_data` normalizes each vote's `council_member` (via `normalize_person_name`) before SQL insert and graph derivation.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_pipeline_typed_write.py
from src.ingestion import pipeline as P
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata

def _chunk():
    m = ChunkMetadata(source_file="r.pdf", department="X", document_type="resolution",
                      quarter="", year=2026, section="", content_type="legal_authorization",
                      page_number=1, parser_used="vision_llm", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text="t", metadata=m)

class _FakeSQL:
    def __init__(self): self.votes = None; self.res = None
    def insert_resolution_rows(self, rows, cid, sf): self.res = rows
    def insert_vote_rows(self, rows, cid, sf): self.votes = rows

class _FakeGraph:
    def __init__(self): self.members = None; self.votes = None; self.res = None
    def upsert_resolutions(self, r): self.res = r
    def upsert_council_members(self, m): self.members = m
    def upsert_votes(self, v): self.votes = v

def test_write_typed_data_normalizes_member_names():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.sql_store = _FakeSQL(); pipe.graph_store = _FakeGraph()
    extracted = {
        "resolutions": [{"resolution_number": "9-2026", "amount": 1.0}],
        "votes": [
            {"resolution_number": "9-2026", "council_member": "Councilman Jones", "vote": "yes"},
            {"resolution_number": "9-2026", "council_member": "  JONES ", "vote": "yes"},
        ],
    }
    pipe._write_typed_data(extracted, [_chunk()], "r.pdf", get_document_type("resolution"))
    # both vote rows normalized to "Jones"
    assert all(v["council_member"] == "Jones" for v in pipe.sql_store.votes)
    # graph members deduped to a single normalized "Jones"
    assert [m["name"] for m in pipe.graph_store.members] == ["Jones"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_pipeline_typed_write.py -v`
Expected: FAIL (member names not normalized; `["Councilman Jones", "Jones"]`-style, or two graph members)

- [ ] **Step 3: Edit `src/ingestion/pipeline.py`**

Add the import near the other ingestion imports:

```python
from src.ingestion.names import normalize_person_name
```

Thread `profile` through. Change `_store_chunks` signature to:

```python
    def _store_chunks(self, chunks, source_file, doc_type, quarantined, profile=None):
```

Update its caller in `ingest_document` (line ~193) to pass the profile:

```python
        self._store_chunks(chunks, path.name, doc_type, quarantined, profile)
```

And the resolution-branch extraction call (line ~290) to forward it:

```python
        extracted = self.sql_extractor.extract_for_type(chunks, doc_type, profile=profile)
```

In `_write_typed_data`, normalize vote member names at the top of the method (right after the docstring / `chunk_id = ...`), so both SQL and graph use canonical names:

```python
        for v in extracted.get("votes", []):
            if v.get("council_member"):
                v["council_member"] = normalize_person_name(v["council_member"])
```

The existing member-derivation (`{v["council_member"] for v in votes ...}`) now produces the deduped normalized set automatically.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ingestion/test_pipeline_typed_write.py -v`
Expected: PASS

- [ ] **Step 5: Confirm no regression + clean import**

Run: `pytest -q -m "not integration"` then `python3 -c "import src.ingestion.pipeline"`
Expected: full unit suite green; import clean.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/pipeline.py tests/ingestion/test_pipeline_typed_write.py
git commit -m "feat(ingestion): thread profile into extraction + normalize vote member names"
```

- [ ] **Step 7: Operator validation (live DB — not run in CI)**

Re-ingest the 7 resolutions and confirm clean data:

```bash
python3 - <<'PY'
import glob, os, logging
logging.basicConfig(level=logging.ERROR)
from src.config import get_settings
from src.ingestion.pipeline import IngestionPipeline
from src.storage.sql_store import SQLStore
cfg = get_settings(); pipe = IngestionPipeline(cfg); pipe.initialize_stores()
for f in [p for p in sorted(glob.glob("docs/Resolutions*.pdf")) if "(1)" not in p]:
    pipe.ingest_document(f)
s = SQLStore(cfg); s.connect()
with s.cursor() as cur:
    cur.execute("SELECT resolution_number, count(*) FROM resolutions GROUP BY resolution_number HAVING count(*)>1")
    print("duplicates (expect none):", cur.fetchall())
    cur.execute("SELECT count(DISTINCT resolution_number) AS c FROM resolutions"); print("distinct resolutions:", cur.fetchone()["c"])
PY
```
Expected: no duplicate resolution_numbers; distinct count matches the unique files. Then re-check the graph `CouncilMember` node count (should drop toward ~7–9) and the dashboard resolutions lane.

---

## Self-Review

**Spec coverage:**
- §4.1 `anchor_field` on DocumentType + registry → Task 1. ✓
- §4.2 anchored extraction (prompt block + deterministic guard) → Task 3. ✓
- §4.3 thread profile through pipeline → Task 4 (steps 3). ✓
- §4.4 `normalize_person_name` + applied before storage → Task 2 (helper) + Task 4 (applied in `_write_typed_data`). ✓
- §4.5 re-ingest validation → Task 4 step 7 (operator). ✓
- §6 testing (anchor-in-prompt, guard collapses to one keyed row, control without profile, name cases, normalization wiring) → Tasks 2/3/4 tests. ✓
- Non-goals (no vision-chunking, no roster matching, QR path unchanged) → respected; Task 3 step 5 guards QR/extractor regression. ✓

**Placeholder scan:** No TBD/TODO; every code step has real code. The operator validation (Task 4 step 7) is explicitly a live-DB step, consistent with the spec.

**Type consistency:** `anchor_field` (Task 1) is read via `getattr(doc_type, "anchor_field", None)` and `doc_type.sql_targets[0]` (Task 3). `extract_for_type(chunks, doc_type, profile=None)` (Task 3) is called with `profile=profile` in Task 4. `normalize_person_name` (Task 2) is imported and applied in Task 4. `_store_chunks(..., profile=None)` (Task 4) matches its `ingest_document` caller. `DocumentProfile.identifying_ids/department/period` used in Task 3 match the model.

**Note:** Task 3 says "refactor the method to assign `result = {...}` then guard then return" — the current code returns the comprehension directly; the implementer must make that small structural change so the guard can run before return.
