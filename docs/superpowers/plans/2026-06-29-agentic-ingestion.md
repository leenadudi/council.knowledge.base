# Agentic, Multi-Type Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded quarterly-report-only ingestion path with a schema-guided agentic pipeline: an LLM profiles each document to determine its type/owner/period, a registry of declared document types drives chunking/classification/extraction, resolutions are added as the first new type, and a bounded worker pool ingests the corpus in parallel.

**Architecture:** A new `profiler` step (LLM, Haiku, first-N-pages) produces a `DocumentProfile`. A `registry` maps each `document_type` to a `DocumentType` spec (chunking hints, content vocabulary, extraction schema, SQL/graph targets). The pipeline looks up the spec and parameterizes the existing chunker/classifier/extractors instead of hardcoding `"quarterly_report"`. Unclassifiable documents are quarantined to the vector store. `ingest_directory` runs documents through a bounded `ThreadPoolExecutor`.

**Tech Stack:** Python 3.11+, Pydantic v2, `anthropic` SDK (via `TrackedAnthropic` wrapper), `psycopg2` (PostgreSQL), `neo4j` driver, `voyageai` embeddings, `pytest`.

## Global Constraints

- All LLM calls go through `TrackedAnthropic` (`src/llm/client.py`) — never call `anthropic.Anthropic` directly. Construct with `call_site="..."`.
- The profiler MUST default to `cfg.profiler_model` (Haiku); extraction/synthesis stay on `cfg.synthesis_model`.
- Never auto-create SQL/graph schema for unknown document types — quarantine to vector store only.
- Bounded concurrency only — cap workers at `cfg.ingest_workers`; never unbounded fan-out.
- Preserve per-document failure isolation: one document failing must not abort the batch.
- Existing public method signatures on `SQLStore`/`GraphStore`/`VectorStore` must keep working (additive changes only).
- Dollar amounts stored as plain numbers (no `$`/commas); dates as `YYYY-MM-DD` or `NULL`.
- Run tests with `pytest` from repo root. Tests that need live Postgres/Neo4j are marked `@pytest.mark.integration`; unit tests stub the stores and LLM.

---

### Task 1: Config + core data models

**Files:**
- Modify: `src/config.py` (add ingestion/profiler settings)
- Modify: `src/models.py` (add `DocumentProfile`, `DocumentType`, `ChunkingHints`; add `needs_review` + profile-sourced fields to `ChunkMetadata`)
- Test: `tests/ingestion/test_models.py`

**Interfaces:**
- Produces:
  - `Settings.profiler_model: str`, `Settings.profiler_max_pages: int`, `Settings.profile_confidence_threshold: float`, `Settings.ingest_workers: int`
  - `DocumentProfile(document_type: str, department: str, period: str, title: str, identifying_ids: dict[str,str], confidence: float, proposed_type: str | None = None)`
  - `ChunkingHints(keep_together: list[str] = [], section_headers: list[str] | None = None)`
  - `DocumentType(name, description, identifying_signals, content_vocab, sql_targets, graph_targets, chunking, extraction_schema, metadata_schema)` (Pydantic model; `extraction_schema`/`metadata_schema` are `type[BaseModel]`)
  - `ChunkMetadata` gains `needs_review: bool = False`; `quarter`/`year` become optional (`quarter: str = ""`, `year: int | None`)

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_models.py
from src.models import DocumentProfile, ChunkingHints
from src.config import get_settings

def test_document_profile_defaults():
    p = DocumentProfile(
        document_type="resolution", department="DEDBH", period="2026",
        title="Resolution 2026-R-12", identifying_ids={"resolution_number": "2026-R-12"},
        confidence=0.91,
    )
    assert p.proposed_type is None
    assert p.identifying_ids["resolution_number"] == "2026-R-12"

def test_chunking_hints_defaults():
    h = ChunkingHints()
    assert h.keep_together == []
    assert h.section_headers is None

def test_settings_have_profiler_defaults():
    cfg = get_settings()
    assert cfg.profiler_model == "claude-haiku-4-5"
    assert cfg.ingest_workers >= 1
    assert 0.0 < cfg.profile_confidence_threshold <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_models.py -v`
Expected: FAIL with `ImportError` / `AttributeError` (DocumentProfile / settings not defined)

- [ ] **Step 3: Add settings to `src/config.py`**

Add inside `Settings` (after the `# Ingestion` block):

```python
    # Agentic profiler (document type/owner/period classification)
    profiler_model: str = "claude-haiku-4-5"   # cheap routing task, eval-validated
    profiler_max_pages: int = 3                 # only the first N pages are read to classify
    profile_confidence_threshold: float = 0.55  # below this → quarantine (vector-only)

    # Parallel ingestion (bounded worker pool over independent documents)
    ingest_workers: int = 5                     # cap to stay under API/DB rate limits
```

- [ ] **Step 4: Add models to `src/models.py`**

Add near the top (after the imports / `CONTENT_TYPES` block):

```python
class DocumentProfile(BaseModel):
    document_type: str
    department: str
    period: str = ""                       # "Q1 2026", "2026", an adopted date, etc.
    title: str = ""
    identifying_ids: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    proposed_type: Optional[str] = None    # set when the agent thinks it's a new, unknown type


class ChunkingHints(BaseModel):
    keep_together: list[str] = Field(default_factory=list)   # marker words whose blocks must not split
    section_headers: Optional[list[str]] = None              # override default section names; None = use defaults


class DocumentType(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    name: str
    description: str
    identifying_signals: list[str] = Field(default_factory=list)
    content_vocab: list[str] = Field(default_factory=list)
    sql_targets: list[str] = Field(default_factory=list)
    graph_targets: list[str] = Field(default_factory=list)
    chunking: ChunkingHints = Field(default_factory=ChunkingHints)
    extraction_schema: Optional[type] = None    # Pydantic model class used as the LLM extraction contract
    metadata_schema: Optional[type] = None
```

Then update `ChunkMetadata` (the dataclass): change `quarter: str` → `quarter: str = ""`, `year: int` → `year: Optional[int] = None`, and add `needs_review: bool = False`. Add `"needs_review": self.needs_review` to `to_dict()`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_models.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/config.py src/models.py tests/ingestion/test_models.py
git commit -m "feat(ingestion): add DocumentProfile/DocumentType models and profiler/worker settings"
```

---

### Task 2: Document Type Registry

**Files:**
- Create: `src/ingestion/registry.py`
- Create: `src/ingestion/schemas/__init__.py`
- Create: `src/ingestion/schemas/quarterly_report.py` (re-uses existing extraction shape)
- Test: `tests/ingestion/test_registry.py`

**Interfaces:**
- Consumes: `DocumentType`, `ChunkingHints` (Task 1)
- Produces:
  - `get_document_type(name: str) -> DocumentType | None`
  - `all_document_types() -> list[DocumentType]`
  - `KNOWN_TYPE_NAMES: list[str]`
  - Registers `"quarterly_report"` now; `"resolution"` is added in Task 7.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_registry.py
import pytest
from src.ingestion.registry import get_document_type, all_document_types, KNOWN_TYPE_NAMES

def test_quarterly_report_registered():
    dt = get_document_type("quarterly_report")
    assert dt is not None
    assert "metrics" in dt.content_vocab or "table" in dt.content_vocab
    assert "expenditures" in dt.sql_targets

def test_unknown_type_returns_none():
    assert get_document_type("nonexistent_type") is None

def test_every_registered_type_is_wellformed():
    for dt in all_document_types():
        assert dt.name and dt.description
        assert dt.content_vocab, f"{dt.name} has empty content_vocab"
        assert dt.name in KNOWN_TYPE_NAMES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: src.ingestion.registry`

- [ ] **Step 3: Create the schemas package + quarterly-report extraction schema**

```python
# src/ingestion/schemas/__init__.py
"""Per-document-type Pydantic extraction/metadata schemas."""
```

```python
# src/ingestion/schemas/quarterly_report.py
"""Extraction contract for quarterly reports — mirrors the existing SQL extractor output."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ExpenditureRow(BaseModel):
    account_number: str = ""
    line_item: str = ""
    sub_department: str = ""
    revised_budget: Optional[float] = None
    ytd_expended: Optional[float] = None
    source_text: str
    confidence: str


class MetricRow(BaseModel):
    metric_name: str
    metric_value: float
    metric_unit: str = "count"
    source_text: str
    confidence: str


class GrantRow(BaseModel):
    grant_name: str
    grant_number: str = ""
    amount: Optional[float] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: str = ""
    source_text: str
    confidence: str


class VacancyRow(BaseModel):
    position_title: str
    status: str
    source_text: str
    confidence: str


class QuarterlyReportExtraction(BaseModel):
    expenditures: list[ExpenditureRow] = Field(default_factory=list)
    metrics: list[MetricRow] = Field(default_factory=list)
    grants: list[GrantRow] = Field(default_factory=list)
    vacancies: list[VacancyRow] = Field(default_factory=list)
```

- [ ] **Step 4: Create the registry**

```python
# src/ingestion/registry.py
"""Declared registry of document types. Adding a new type = adding an entry here
plus its Pydantic schema(s) and any new SQL tables / graph nodes. No pipeline edits."""
from __future__ import annotations

from src.models import DocumentType, ChunkingHints
from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction

_QUARTERLY_REPORT = DocumentType(
    name="quarterly_report",
    description=("A city department's quarterly report: description, quarterly summary, "
                 "metrics/counts, budget/expenditure tables, annual goals, special projects, "
                 "vacancies, community engagement."),
    identifying_signals=["quarterly report", "Q1", "Q2", "Q3", "Q4", "year-to-date", "annual goals"],
    content_vocab=["narrative", "table", "metrics", "org_data", "project", "header"],
    sql_targets=["expenditures", "metrics", "grants", "vacancies"],
    graph_targets=["Department", "Person", "Project", "Grant"],
    chunking=ChunkingHints(),  # use default section-aware chunking
    extraction_schema=QuarterlyReportExtraction,
)

_REGISTRY: dict[str, DocumentType] = {
    _QUARTERLY_REPORT.name: _QUARTERLY_REPORT,
}


def register(dt: DocumentType) -> None:
    _REGISTRY[dt.name] = dt


def get_document_type(name: str) -> DocumentType | None:
    return _REGISTRY.get(name)


def all_document_types() -> list[DocumentType]:
    return list(_REGISTRY.values())


KNOWN_TYPE_NAMES: list[str] = list(_REGISTRY.keys())
```

Note: `KNOWN_TYPE_NAMES` is computed once at import. After Task 7 registers resolutions at import time, recompute it as `list(_REGISTRY.keys())` *after* all `register()` calls, or expose it as a function. For now keep it as the module-level list and re-evaluate in Task 7.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_registry.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/registry.py src/ingestion/schemas/ tests/ingestion/test_registry.py
git commit -m "feat(ingestion): add document-type registry with quarterly_report entry"
```

---

### Task 3: Agentic document profiler

**Files:**
- Create: `src/ingestion/profiler.py`
- Test: `tests/ingestion/test_profiler.py`

**Interfaces:**
- Consumes: `DocumentProfile` (Task 1), `KNOWN_TYPE_NAMES`/`all_document_types` (Task 2), `TrackedAnthropic`, `ParsedDocument`
- Produces:
  - `profile_document(parsed: ParsedDocument, source_file: str, category_hint: str | None = None, client: TrackedAnthropic | None = None, settings: Settings | None = None) -> DocumentProfile`
  - Reads only the first `cfg.profiler_max_pages` pages; uses `cfg.profiler_model`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_profiler.py
import json
from src.ingestion.profiler import profile_document
from src.models import ParsedDocument, ParsedElement

class _FakeMsg:
    def __init__(self, text): self.content = [type("C", (), {"text": text})()]

class _FakeClient:
    def __init__(self, payload): self._payload = payload; self.calls = []
        # messages.create(**kwargs)
    class _M:
        def __init__(self, outer): self._outer = outer
        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return _FakeMsg(self._outer._payload)
    @property
    def messages(self): return _FakeClient._M(self)

def _parsed(text):
    return ParsedDocument(source_file="x.pdf", parser_used="unstructured",
                          elements=[ParsedElement("NarrativeText", text, 1)], total_pages=1)

def test_profiler_returns_known_type():
    payload = json.dumps({"document_type": "resolution", "department": "DEDBH",
                          "period": "2026-03-03", "title": "RES 2026-R-12",
                          "identifying_ids": {"resolution_number": "2026-R-12"},
                          "confidence": 0.92})
    p = profile_document(_parsed("RESOLUTION NO 2026-R-12 ... WHEREAS ... RESOLVED"),
                         "res12.pdf", client=_FakeClient(payload))
    assert p.document_type == "resolution"
    assert p.confidence == 0.92

def test_profiler_uses_profiler_model_and_first_pages():
    payload = json.dumps({"document_type": "quarterly_report", "department": "Health Office",
                          "period": "Q1 2026", "title": "", "identifying_ids": {}, "confidence": 0.8})
    c = _FakeClient(payload)
    profile_document(_parsed("Quarterly Report Health Office"), "h.pdf", client=c)
    assert c.calls[0]["model"].startswith("claude-haiku")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_profiler.py -v`
Expected: FAIL with `ModuleNotFoundError: src.ingestion.profiler`

- [ ] **Step 3: Implement the profiler**

```python
# src/ingestion/profiler.py
"""Agentic document profiling: read the first few pages, return a DocumentProfile
(type, department/owner, period, ids, confidence). Replaces filename-regex metadata."""
from __future__ import annotations

import json
import logging
from typing import Optional

from src.config import Settings, get_settings
from src.llm.client import TrackedAnthropic
from src.models import DocumentProfile, ParsedDocument
from src.ingestion.registry import all_document_types

logger = logging.getLogger(__name__)

_PROMPT = """You classify City of Harrisburg government documents. Read the excerpt and identify what it is.

Known document types (choose the single best match by name):
{type_menu}

Rules:
- "document_type" MUST be one of the known type names above, OR "unclassified" if none fit.
- If it looks like a real type not in the list, set "document_type" to "unclassified" and put your guess in "proposed_type".
- "department" is the owning city department/bureau/office, or the body that issued it.
- "period" is the time it covers: a quarter like "Q1 2026", a year "2026", or an adoption date "YYYY-MM-DD".
- "identifying_ids" holds stable identifiers found in the text (e.g. {{"resolution_number": "2026-R-12"}}).
- "confidence" is 0.0-1.0 — how sure you are of the document_type.
{hint_line}
Filename (weak hint only, may be misleading): {source_file}

Return ONLY a JSON object:
{{"document_type": "...", "department": "...", "period": "...", "title": "...",
  "identifying_ids": {{}}, "confidence": 0.0, "proposed_type": null}}

Document excerpt:
---
{excerpt}
---"""


def _excerpt(parsed: ParsedDocument, max_pages: int) -> str:
    parts = [e.text for e in parsed.elements if e.page_number <= max_pages]
    return "\n\n".join(parts)[:8000]


def profile_document(
    parsed: ParsedDocument,
    source_file: str,
    category_hint: Optional[str] = None,
    client: Optional[TrackedAnthropic] = None,
    settings: Optional[Settings] = None,
) -> DocumentProfile:
    cfg = settings or get_settings()
    llm = client or TrackedAnthropic(cfg, call_site="ingestion.profiler")

    type_menu = "\n".join(f"- {dt.name}: {dt.description}" for dt in all_document_types())
    hint_line = (f"- A source-system category hint is provided (treat as strong but verifiable): "
                 f"\"{category_hint}\".\n") if category_hint else ""
    prompt = _PROMPT.format(
        type_menu=type_menu, hint_line=hint_line, source_file=source_file,
        excerpt=_excerpt(parsed, cfg.profiler_max_pages),
    )

    try:
        msg = llm.messages.create(
            model=cfg.profiler_model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
        return DocumentProfile(
            document_type=data.get("document_type", "unclassified") or "unclassified",
            department=data.get("department", "") or "",
            period=data.get("period", "") or "",
            title=data.get("title", "") or "",
            identifying_ids=data.get("identifying_ids", {}) or {},
            confidence=float(data.get("confidence", 0.0) or 0.0),
            proposed_type=data.get("proposed_type"),
        )
    except Exception as e:
        logger.warning("Profiler failed for %s: %s — marking unclassified", source_file, e)
        return DocumentProfile(document_type="unclassified", department="", confidence=0.0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_profiler.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/profiler.py tests/ingestion/test_profiler.py
git commit -m "feat(ingestion): add agentic document profiler (Haiku, first-N-pages)"
```

---

### Task 4: Demote filename metadata to a hint; source metadata from the profile

**Files:**
- Modify: `src/ingestion/metadata.py`
- Test: `tests/ingestion/test_metadata.py`

**Interfaces:**
- Consumes: `DocumentProfile` (Task 1)
- Produces:
  - `filename_hint(source_file: str) -> dict` (the OLD `extract_file_metadata` behavior, renamed; used only as a profiler hint / fallback)
  - `build_chunk_metadata(chunk_dict, source_file, chunk_index, total_chunks, content_type, parser_used, profile: DocumentProfile, needs_review: bool = False) -> dict` (now takes the profile)

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_metadata.py
from src.ingestion.metadata import build_chunk_metadata, filename_hint
from src.models import DocumentProfile

def test_metadata_comes_from_profile_not_filename():
    profile = DocumentProfile(document_type="resolution", department="DEDBH",
                              period="2026-03-03", title="RES", confidence=0.9)
    meta = build_chunk_metadata(
        chunk_dict={"section": "RESOLVED", "page_number": 1},
        source_file="whatever_random_name.pdf", chunk_index=0, total_chunks=3,
        content_type="legal_authorization", parser_used="unstructured", profile=profile,
    )
    assert meta["document_type"] == "resolution"
    assert meta["department"] == "DEDBH"
    assert meta["needs_review"] is False

def test_filename_hint_still_parses_quarterly_convention():
    hint = filename_hint("Misc. Documents - Quarterly Reports - 2026 - Bureau of Codes_Q1 2026.pdf")
    assert hint["quarter"] == "Q1"
    assert hint["year"] == 2026
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_metadata.py -v`
Expected: FAIL (`filename_hint` undefined; `build_chunk_metadata` signature mismatch)

- [ ] **Step 3: Edit `src/ingestion/metadata.py`**

Rename `extract_file_metadata` → `filename_hint` (keep its body verbatim). Rewrite `build_chunk_metadata`:

```python
def build_chunk_metadata(
    chunk_dict: dict,
    source_file: str,
    chunk_index: int,
    total_chunks: int,
    content_type: str,
    parser_used: str,
    profile,                       # DocumentProfile
    needs_review: bool = False,
) -> dict:
    """Assemble chunk metadata. Type/department/period come from the profile,
    NOT the filename. Filename is retained only as source_file."""
    # period may be "Q1 2026" or a date/year — split out quarter/year best-effort
    quarter, year = _split_period(profile.period)
    return {
        "source_file": source_file,
        "department": profile.department or "Unknown Department",
        "document_type": profile.document_type,
        "quarter": quarter,
        "year": year,
        "section": chunk_dict.get("section", ""),
        "content_type": content_type,
        "page_number": chunk_dict.get("page_number", 1),
        "parser_used": parser_used,
        "ingestion_timestamp": datetime.utcnow().isoformat(),
        "chunk_index": chunk_index,
        "total_chunks_in_doc": total_chunks,
        "needs_review": needs_review,
    }


def _split_period(period: str) -> tuple[str, "int | None"]:
    """Best-effort: pull a quarter (Qn) and a 4-digit year out of a period string."""
    quarter = ""
    year = None
    if period:
        qm = re.search(r"Q([1-4])", period, re.IGNORECASE)
        if qm:
            quarter = f"Q{qm.group(1)}"
        ym = re.search(r"\b(20\d{2})\b", period)
        if ym:
            year = int(ym.group(1))
    return quarter, year
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_metadata.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/metadata.py tests/ingestion/test_metadata.py
git commit -m "refactor(ingestion): source chunk metadata from DocumentProfile; filename is hint-only"
```

---

### Task 5: Chunker honors registry chunking hints

**Files:**
- Modify: `src/ingestion/chunker.py`
- Test: `tests/ingestion/test_chunker_hints.py`

**Interfaces:**
- Consumes: `ChunkingHints` (Task 1)
- Produces: `chunk_document(parsed, settings=None, hints: ChunkingHints | None = None) -> list[dict]`
  - When `hints.keep_together` is set, any element whose text contains a marker word starts/extends a buffer that is NOT split until a non-matching boundary — keeps WHEREAS+RESOLVED blocks intact.
  - When `hints.section_headers` is set, it overrides `_SECTION_HEADERS` for boundary detection.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_chunker_hints.py
from src.ingestion.chunker import chunk_document
from src.models import ParsedDocument, ParsedElement, ChunkingHints

def _doc(elems):
    return ParsedDocument(source_file="r.pdf", parser_used="unstructured",
                          elements=elems, total_pages=1)

def test_keep_together_does_not_split_whereas_resolved():
    big_whereas = "WHEREAS " + ("the city finds it necessary " * 200)
    resolved = "RESOLVED that the contract is authorized."
    doc = _doc([ParsedElement("NarrativeText", big_whereas, 1),
                ParsedElement("NarrativeText", resolved, 1)])
    hints = ChunkingHints(keep_together=["whereas", "resolved"])
    chunks = chunk_document(doc, hints=hints)
    # the RESOLVED conclusion must live in the same chunk as its WHEREAS reasoning
    joined = [c for c in chunks if "RESOLVED that the contract" in c["text"]]
    assert joined and "WHEREAS" in joined[0]["text"]

def test_default_behavior_unchanged_without_hints():
    doc = _doc([ParsedElement("Title", "Budget", 1),
                ParsedElement("Table", "Account 100 ... 5000.00", 1)])
    chunks = chunk_document(doc)  # no hints → existing path
    assert any(c["element_type"] == "Table" for c in chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_chunker_hints.py -v`
Expected: FAIL (`chunk_document` has no `hints` kwarg)

- [ ] **Step 3: Edit `src/ingestion/chunker.py`**

Change the signature and branch on hints:

```python
def chunk_document(parsed, settings=None, hints=None):
    cfg = settings or get_settings()
    if parsed.parser_used == "vision_llm":
        return _chunk_slide_deck(parsed, cfg.min_chunk_size)
    if hints and hints.keep_together:
        return _chunk_keep_together(parsed, cfg, hints.keep_together)
    return _chunk_by_sections(parsed, cfg.max_chunk_size, cfg.min_chunk_size, cfg.chunk_overlap)
```

Add the new function (groups consecutive elements into one chunk once a marker is seen, flushing only when a new marker block starts):

```python
def _chunk_keep_together(parsed, cfg, markers: list[str]) -> list[dict]:
    markers_lc = [m.lower() for m in markers]
    chunks: list[dict] = []
    buf = None  # _ChunkBuffer

    def starts_block(text: str) -> bool:
        head = text[:60].lower()
        return any(m in head for m in markers_lc)

    def flush(b):
        if b and b.parts and len(b.text) >= cfg.min_chunk_size:
            chunks.append({"text": b.text, "section": b.section, "page_number": b.page,
                           "element_type": b.dominant_type, "parser_used": parsed.parser_used})

    for elem in parsed.elements:
        if starts_block(elem.text):
            flush(buf)
            buf = _ChunkBuffer(elem.text.strip()[:80], elem.page_number)
            buf.add(elem.text, elem.element_type)
        elif buf is not None:
            buf.add(elem.text, elem.element_type)
        else:
            buf = _ChunkBuffer("Preamble", elem.page_number)
            buf.add(elem.text, elem.element_type)
    flush(buf)
    return chunks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_chunker_hints.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/chunker.py tests/ingestion/test_chunker_hints.py
git commit -m "feat(ingestion): chunker honors registry keep-together hints"
```

---

### Task 6: Classifier uses per-type content vocabulary

**Files:**
- Modify: `src/ingestion/classifier.py`
- Test: `tests/ingestion/test_classifier_vocab.py`

**Interfaces:**
- Consumes: nothing new (vocab passed in)
- Produces: `classify_chunk(chunk_dict, element_type, client=None, settings=None, vocab: list[str] | None = None) -> str` and `classify_batch(chunk_dicts, element_types, settings=None, vocab: list[str] | None = None) -> list[str]`
  - When `vocab` is provided, the LLM-fallback prompt lists those categories and the result is validated against `vocab` (falling back to the first vocab entry). When `vocab` is None, behavior is unchanged (uses `CONTENT_TYPES`).

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_classifier_vocab.py
from src.ingestion.classifier import _validate_against_vocab

def test_validate_against_vocab_passthrough():
    assert _validate_against_vocab("vote_record", ["legal_authorization", "vote_record"]) == "vote_record"

def test_validate_against_vocab_falls_back_to_first():
    assert _validate_against_vocab("garbage", ["legal_authorization", "vote_record"]) == "legal_authorization"

def test_validate_against_vocab_none_uses_content_types():
    from src.models import CONTENT_TYPES
    assert _validate_against_vocab("table", None) == "table"
    assert _validate_against_vocab("garbage", None) == "narrative"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_classifier_vocab.py -v`
Expected: FAIL (`_validate_against_vocab` undefined)

- [ ] **Step 3: Edit `src/ingestion/classifier.py`**

Add the helper and thread `vocab` through:

```python
def _validate_against_vocab(result: str, vocab):
    result = (result or "").strip().lower()
    allowed = vocab if vocab else list(CONTENT_TYPES)
    if result in allowed:
        return result
    return allowed[0] if vocab else "narrative"
```

In `_llm_classify`, accept `vocab` and build the category list dynamically; replace the final validation with `return _validate_against_vocab(result, vocab)`. Add `vocab=None` params to `classify_chunk`/`classify_batch` and pass through. When `vocab` is set, **skip** `_rule_based` (its categories are quarterly-report-specific) and go straight to the LLM with the type's vocabulary.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_classifier_vocab.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/classifier.py tests/ingestion/test_classifier_vocab.py
git commit -m "feat(ingestion): classifier accepts per-type content vocabulary"
```

---

### Task 7: Resolution schema + SQL tables + store methods + registry entry

**Files:**
- Create: `src/ingestion/schemas/resolution.py`
- Modify: `sql/schema.sql` (add `resolutions`, `votes`)
- Modify: `src/storage/sql_store.py` (add `insert_resolution_rows`, `insert_vote_rows`)
- Modify: `src/ingestion/registry.py` (register `resolution`)
- Test: `tests/ingestion/test_resolution_schema.py`, `tests/storage/test_resolution_store.py` (integration)

**Interfaces:**
- Consumes: `DocumentType`, `ChunkingHints`, registry `register()`
- Produces:
  - `ResolutionExtraction` Pydantic model with `resolutions: list[ResolutionRow]`, `votes: list[VoteRow]`
  - `SQLStore.insert_resolution_rows(rows: list[dict], source_chunk_id: str, source_file: str) -> None`
  - `SQLStore.insert_vote_rows(rows: list[dict], source_chunk_id: str, source_file: str) -> None`
  - registry now contains `"resolution"` with `sql_targets=["resolutions","votes"]`, `graph_targets=["Resolution","Vendor","CouncilMember"]`, `chunking=ChunkingHints(keep_together=["whereas","resolved"])`

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_resolution_schema.py
from src.ingestion.registry import get_document_type
from src.ingestion.schemas.resolution import ResolutionExtraction

def test_resolution_registered_with_keep_together():
    dt = get_document_type("resolution")
    assert dt is not None
    assert "resolutions" in dt.sql_targets and "votes" in dt.sql_targets
    assert dt.chunking.keep_together == ["whereas", "resolved"]
    assert dt.extraction_schema is ResolutionExtraction

def test_resolution_extraction_parses():
    e = ResolutionExtraction.model_validate({
        "resolutions": [{"resolution_number": "2026-R-12", "title": "Award",
                         "amount": 40000.0, "vendor": "Acme", "adopted_date": "2026-03-03",
                         "status": "adopted", "source_text": "RESOLVED...", "confidence": "high"}],
        "votes": [{"resolution_number": "2026-R-12", "council_member": "Smith",
                   "vote": "yes", "source_text": "Smith - yes", "confidence": "high"}],
    })
    assert e.resolutions[0].amount == 40000.0
    assert e.votes[0].vote == "yes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_resolution_schema.py -v`
Expected: FAIL (`src.ingestion.schemas.resolution` missing; resolution not registered)

- [ ] **Step 3: Create the resolution schema**

```python
# src/ingestion/schemas/resolution.py
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ResolutionRow(BaseModel):
    resolution_number: str
    title: str = ""
    amount: Optional[float] = None
    vendor: str = ""
    department: str = ""
    adopted_date: Optional[str] = None
    status: str = ""
    source_text: str
    confidence: str


class VoteRow(BaseModel):
    resolution_number: str
    council_member: str
    vote: str            # "yes" | "no" | "abstain"
    source_text: str
    confidence: str


class ResolutionExtraction(BaseModel):
    resolutions: list[ResolutionRow] = Field(default_factory=list)
    votes: list[VoteRow] = Field(default_factory=list)
```

- [ ] **Step 4: Add tables to `sql/schema.sql`**

```sql
CREATE TABLE IF NOT EXISTS resolutions (
    id                 SERIAL PRIMARY KEY,
    resolution_number  VARCHAR(50),
    title              TEXT,
    amount             DECIMAL(15,2),
    vendor             VARCHAR(255),
    department         VARCHAR(100),
    adopted_date       DATE,
    status             VARCHAR(50),
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS votes (
    id                 SERIAL PRIMARY KEY,
    resolution_number  VARCHAR(50),
    council_member     VARCHAR(120),
    vote               VARCHAR(10),
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);
```

- [ ] **Step 5: Add store methods to `src/storage/sql_store.py`**

Following the exact pattern of `insert_grant_rows`:

```python
    def insert_resolution_rows(self, rows, source_chunk_id, source_file):
        sql = """
            INSERT INTO resolutions
              (resolution_number, title, amount, vendor, department, adopted_date,
               status, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("resolution_number"), r.get("title"), r.get("amount"),
                    r.get("vendor"), r.get("department"), r.get("adopted_date") or None,
                    r.get("status"), source_chunk_id, source_file,
                ))

    def insert_vote_rows(self, rows, source_chunk_id, source_file):
        sql = """
            INSERT INTO votes
              (resolution_number, council_member, vote, source_chunk_id, source_file)
            VALUES (%s,%s,%s,%s,%s)
        """
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("resolution_number"), r.get("council_member"),
                    r.get("vote"), source_chunk_id, source_file,
                ))
```

- [ ] **Step 6: Register the resolution type in `src/ingestion/registry.py`**

```python
from src.ingestion.schemas.resolution import ResolutionExtraction

_RESOLUTION = DocumentType(
    name="resolution",
    description=("A formal City Council action authorizing a contract, expenditure, or policy. "
                 "Has a RESOLUTION NO., WHEREAS reasoning clauses, a RESOLVED authorization, "
                 "an adoption date, and a vote record by council member."),
    identifying_signals=["RESOLUTION NO", "WHEREAS", "RESOLVED", "BE IT RESOLVED"],
    content_vocab=["legal_authorization", "whereas_clause", "vote_record", "narrative", "header"],
    sql_targets=["resolutions", "votes"],
    graph_targets=["Resolution", "Vendor", "CouncilMember"],
    chunking=ChunkingHints(keep_together=["whereas", "resolved"]),
    extraction_schema=ResolutionExtraction,
)
register(_RESOLUTION)
KNOWN_TYPE_NAMES = list(_REGISTRY.keys())  # recompute after all registrations
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/ingestion/test_resolution_schema.py -v`
Expected: PASS (2 tests)
For the integration store test (requires Postgres): `pytest tests/storage/test_resolution_store.py -v -m integration` — apply `sql/schema.sql` first.

- [ ] **Step 8: Commit**

```bash
git add src/ingestion/schemas/resolution.py sql/schema.sql src/storage/sql_store.py src/ingestion/registry.py tests/ingestion/test_resolution_schema.py tests/storage/test_resolution_store.py
git commit -m "feat(ingestion): add resolution type, schema, SQL tables, and store methods"
```

---

### Task 8: Graph store — Resolution / Vendor / CouncilMember nodes

**Files:**
- Modify: `src/storage/graph_store.py` (add `upsert_resolutions`, `upsert_vendors`, `upsert_council_members`, `upsert_votes`; extend `ensure_constraints`)
- Test: `tests/storage/test_resolution_graph.py` (integration)

**Interfaces:**
- Produces:
  - `GraphStore.upsert_resolutions(resolutions: list[dict]) -> None` (props: resolution_number, title, amount, status, adopted_date)
  - `GraphStore.upsert_vendors(vendors: list[dict]) -> None` (props: name)
  - `GraphStore.upsert_council_members(members: list[dict]) -> None` (props: name)
  - `GraphStore.upsert_votes(votes: list[dict]) -> None` (creates `(CouncilMember)-[:VOTED {vote}]->(Resolution)`)
  - Relationship from resolution → vendor: `(Resolution)-[:AWARDS_CONTRACT_TO]->(Vendor)` written inside `upsert_resolutions` when `vendor` present.

- [ ] **Step 1: Write the failing test (integration)**

```python
# tests/storage/test_resolution_graph.py
import pytest
from src.storage.graph_store import GraphStore

@pytest.mark.integration
def test_upsert_resolution_and_vote_roundtrip():
    g = GraphStore(); g.connect(); g.ensure_constraints()
    g.upsert_resolutions([{"resolution_number": "2026-R-99", "title": "T",
                           "amount": 1000.0, "status": "adopted",
                           "adopted_date": "2026-03-03", "vendor": "Acme"}])
    g.upsert_council_members([{"name": "Smith"}])
    g.upsert_votes([{"resolution_number": "2026-R-99", "council_member": "Smith", "vote": "yes"}])
    rows = g.execute_cypher(
        "MATCH (c:CouncilMember)-[v:VOTED]->(r:Resolution {resolution_number:'2026-R-99'}) "
        "RETURN c.name AS m, v.vote AS vote")
    assert rows and rows[0]["vote"] == "yes"
    g.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/storage/test_resolution_graph.py -v -m integration`
Expected: FAIL (`upsert_resolutions` undefined)

- [ ] **Step 3: Implement the methods in `src/storage/graph_store.py`**

Follow the `_run(cypher, params)` + `UNWIND` pattern used by `upsert_grants`:

```python
    def upsert_resolutions(self, resolutions):
        self._run("""
            UNWIND $rows AS row
            MERGE (r:Resolution {resolution_number: row.resolution_number})
            SET r.title = row.title, r.amount = row.amount,
                r.status = row.status, r.adopted_date = row.adopted_date
            WITH r, row WHERE row.vendor IS NOT NULL AND row.vendor <> ''
            MERGE (v:Vendor {name: row.vendor})
            MERGE (r)-[:AWARDS_CONTRACT_TO]->(v)
        """, {"rows": resolutions})

    def upsert_vendors(self, vendors):
        self._run("UNWIND $rows AS row MERGE (:Vendor {name: row.name})", {"rows": vendors})

    def upsert_council_members(self, members):
        self._run("UNWIND $rows AS row MERGE (:CouncilMember {name: row.name})", {"rows": members})

    def upsert_votes(self, votes):
        self._run("""
            UNWIND $rows AS row
            MERGE (c:CouncilMember {name: row.council_member})
            MERGE (r:Resolution {resolution_number: row.resolution_number})
            MERGE (c)-[v:VOTED]->(r)
            SET v.vote = row.vote
        """, {"rows": votes})
```

Add constraints in `ensure_constraints` for `Resolution.resolution_number`, `Vendor.name`, `CouncilMember.name` (mirror existing constraint statements).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/storage/test_resolution_graph.py -v -m integration`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/storage/graph_store.py tests/storage/test_resolution_graph.py
git commit -m "feat(graph): add Resolution/Vendor/CouncilMember nodes and VOTED relationships"
```

---

### Task 9: Schema-driven extraction routed by the registry

**Files:**
- Modify: `src/extraction/sql_extractor.py` (add `extract_for_type(chunks, doc_type: DocumentType)`; build prompt from `doc_type.extraction_schema`)
- Test: `tests/extraction/test_schema_driven_extraction.py`

**Interfaces:**
- Consumes: `DocumentType` (registry), `ResolutionExtraction`/`QuarterlyReportExtraction`
- Produces:
  - `SQLExtractor.extract_for_type(chunks: list[Chunk], doc_type: DocumentType) -> dict[str, list[dict]]` — returns a dict keyed by the doc_type's `sql_targets` (e.g. `{"resolutions": [...], "votes": [...]}`), validated against `doc_type.extraction_schema`.
  - Existing `extract_chunks_batched` stays as the quarterly-report default (back-compat).

- [ ] **Step 1: Write the failing test**

```python
# tests/extraction/test_schema_driven_extraction.py
import json
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata

class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]
class _FakeClient:
    def __init__(self, payload): self._p = payload
    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k): return _FakeMsg(self._o._p)
    @property
    def messages(self): return _FakeClient._M(self)

def _chunk(text):
    m = ChunkMetadata(source_file="r.pdf", department="DEDBH", document_type="resolution",
                      quarter="", year=2026, section="RESOLVED", content_type="legal_authorization",
                      page_number=1, parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text=text, metadata=m)

def test_extract_for_type_resolution():
    payload = json.dumps({"resolutions": [{"resolution_number": "2026-R-12", "amount": 40000.0,
                          "vendor": "Acme", "source_text": "RESOLVED", "confidence": "high"}],
                          "votes": []})
    ext = SQLExtractor(llm=_FakeClient(payload))
    out = ext.extract_for_type([_chunk("RESOLVED ... $40,000 to Acme")], get_document_type("resolution"))
    assert out["resolutions"][0]["resolution_number"] == "2026-R-12"
    assert out["resolutions"][0]["amount"] == 40000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/extraction/test_schema_driven_extraction.py -v`
Expected: FAIL (`extract_for_type` undefined)

- [ ] **Step 3: Implement `extract_for_type`**

Build the prompt from the schema's JSON shape and validate the response with the Pydantic model:

```python
    def extract_for_type(self, chunks, doc_type):
        if not chunks or doc_type is None or doc_type.extraction_schema is None:
            return {}
        text = "\n\n---\n\n".join(c.text for c in chunks)
        schema_json = json.dumps(doc_type.extraction_schema.model_json_schema())
        prompt = (
            f"You are a precise data extractor for City of Harrisburg '{doc_type.name}' documents.\n"
            f"Extract structured data matching THIS JSON schema (return an object with these keys):\n"
            f"{schema_json}\n\n"
            "Rules: include a verbatim 'source_text' for every row; set 'confidence' to high|medium|low "
            "and omit low-confidence rows; dollar amounts as plain numbers; dates YYYY-MM-DD or null. "
            "Return ONLY the JSON object.\n\nText:\n---\n" + text + "\n---"
        )
        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model, max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            validated = doc_type.extraction_schema.model_validate_json(raw)
            data = validated.model_dump()
            # keep only the doc_type's declared sql_targets, drop low-confidence rows
            return {k: [r for r in v if r.get("confidence") in ("high", "medium")]
                    for k, v in data.items() if k in doc_type.sql_targets and v}
        except Exception as e:
            logger.warning("schema-driven extraction failed for %s: %s", doc_type.name, e)
            return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/extraction/test_schema_driven_extraction.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/extraction/sql_extractor.py tests/extraction/test_schema_driven_extraction.py
git commit -m "feat(extraction): registry-schema-driven SQL extraction (extract_for_type)"
```

---

### Task 10: Wire profiler + registry + quarantine into the pipeline

**Files:**
- Modify: `src/ingestion/pipeline.py` (`ingest_document` and `_store_chunks`/`_write_sql_data`)
- Test: `tests/ingestion/test_pipeline_routing.py`

**Interfaces:**
- Consumes: `profile_document` (Task 3), `get_document_type` (Task 2), `build_chunk_metadata(profile=...)` (Task 4), `chunk_document(hints=...)` (Task 5), `classify_batch(vocab=...)` (Task 6), `extract_for_type` (Task 9), resolution store methods (Task 7/8)
- Produces: `IngestionPipeline.ingest_document(file_path, category_hint=None) -> list[Chunk]`
  - Profiles → looks up type → low confidence/`unclassified` → quarantine (vector-only, `needs_review=True`, `document_type` recorded). Known type → chunk with hints, classify with vocab, extract with schema, route to that type's targets.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_pipeline_routing.py
from src.models import DocumentProfile

def test_low_confidence_is_quarantined(monkeypatch):
    from src.ingestion import pipeline as P
    captured = {}
    # stub the profiler to return low confidence
    monkeypatch.setattr(P, "profile_document",
        lambda *a, **k: DocumentProfile(document_type="resolution", department="X", confidence=0.10))
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)  # skip __init__/stores
    assert pipe._is_quarantined(DocumentProfile(document_type="resolution", department="X", confidence=0.10)) is True
    assert pipe._is_quarantined(DocumentProfile(document_type="resolution", department="X", confidence=0.90)) is False
    assert pipe._is_quarantined(DocumentProfile(document_type="unclassified", department="", confidence=0.99)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_pipeline_routing.py -v`
Expected: FAIL (`_is_quarantined` undefined)

- [ ] **Step 3: Edit `src/ingestion/pipeline.py`**

Add imports (`from src.ingestion.profiler import profile_document`, `from src.ingestion.registry import get_document_type`). Add the guard:

```python
    def _is_quarantined(self, profile) -> bool:
        if profile.document_type == "unclassified":
            return True
        if get_document_type(profile.document_type) is None:
            return True
        return profile.confidence < self.cfg.profile_confidence_threshold
```

Rewrite `ingest_document` to: parse → `profile = profile_document(parsed, path.name, category_hint, settings=self.cfg)` → `quarantined = self._is_quarantined(profile)` → `doc_type = get_document_type(profile.document_type) if not quarantined else None` → chunk with `hints=doc_type.chunking if doc_type else None` → `vocab = doc_type.content_vocab if doc_type else None` for `classify_batch` → build metadata with `profile=profile, needs_review=quarantined` → embed → `self._store_chunks(chunks, path.name, doc_type, quarantined)`. Replace the hardcoded `document_type="quarterly_report"` in `record_document` with `profile.document_type`.

In `_store_chunks`, when `quarantined` is True, write **only** the vector store and return. Otherwise route SQL via `self.sql_extractor.extract_for_type(sql_chunks, doc_type)` and dispatch each returned key to the matching store method (`expenditures→insert_expenditure_rows`, …, `resolutions→insert_resolution_rows`, `votes→insert_vote_rows`). Keep graph routing for quarterly_report as-is; for resolution call `upsert_resolutions`/`upsert_council_members`/`upsert_votes` from the graph extraction (graph extraction for resolutions can reuse `extract_for_type` results: resolutions/votes feed the graph methods directly).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ingestion/test_pipeline_routing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/pipeline.py tests/ingestion/test_pipeline_routing.py
git commit -m "feat(ingestion): wire profiler+registry+quarantine into pipeline"
```

---

### Task 11: Bounded-concurrency document loader + concurrent classification

**Files:**
- Modify: `src/ingestion/pipeline.py` (`ingest_directory`)
- Modify: `src/ingestion/classifier.py` (`classify_batch` concurrency)
- Test: `tests/ingestion/test_concurrent_ingest.py`

**Interfaces:**
- Produces:
  - `IngestionPipeline.ingest_directory(docs_dir, skip_existing=True, max_workers: int | None = None) -> None` — uses `ThreadPoolExecutor(max_workers=max_workers or cfg.ingest_workers)`; per-document exceptions are caught and logged (batch continues); a 429/rate-limit triggers bounded retry with backoff.
  - `classify_batch` runs its per-chunk LLM calls via a thread pool capped at `cfg.ingest_workers`, preserving input order.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_concurrent_ingest.py
import time
from src.ingestion.classifier import classify_batch

def test_classify_batch_preserves_order_and_runs_concurrently(monkeypatch):
    # stub classify_chunk to sleep, so concurrency is observable and order must hold
    from src.ingestion import classifier as C
    def fake(chunk_dict, element_type, client=None, settings=None, vocab=None):
        time.sleep(0.05)
        return chunk_dict["text"]  # echo so we can assert order
    monkeypatch.setattr(C, "classify_chunk", fake)
    chunks = [{"text": f"c{i}"} for i in range(8)]
    t0 = time.time()
    out = classify_batch(chunks, ["NarrativeText"] * 8)
    elapsed = time.time() - t0
    assert out == [f"c{i}" for i in range(8)]          # order preserved
    assert elapsed < 0.05 * 8 * 0.6                    # ran concurrently, not serially
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_concurrent_ingest.py -v`
Expected: FAIL (serial implementation too slow → assertion error on `elapsed`)

- [ ] **Step 3: Make `classify_batch` concurrent (order-preserving)**

```python
from concurrent.futures import ThreadPoolExecutor

def classify_batch(chunk_dicts, element_types, settings=None, vocab=None):
    cfg = settings or get_settings()
    client = _make_llm(cfg)
    workers = max(1, min(cfg.ingest_workers, len(chunk_dicts)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(
            lambda pair: classify_chunk(pair[0], pair[1], client=client, settings=cfg, vocab=vocab),
            list(zip(chunk_dicts, element_types)),
        ))
    return results
```

- [ ] **Step 4: Make `ingest_directory` use a worker pool with failure isolation + backoff**

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def ingest_directory(self, docs_dir, skip_existing=True, max_workers=None):
    path = Path(docs_dir)
    pdfs = sorted(path.glob("*.pdf"))
    todo = [p for p in pdfs if not (skip_existing and self.sql_store.is_document_ingested(p.name))]
    logger.info("Ingesting %d/%d documents with %d workers",
                len(todo), len(pdfs), max_workers or self.cfg.ingest_workers)
    with ThreadPoolExecutor(max_workers=max_workers or self.cfg.ingest_workers) as ex:
        futures = {ex.submit(self._ingest_one_safe, p): p for p in todo}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                fut.result()
            except Exception as e:
                logger.error("Failed to ingest %s: %s", p.name, e, exc_info=True)

def _ingest_one_safe(self, path, attempts=3):
    import time as _t
    for i in range(attempts):
        try:
            return self.ingest_document(path)
        except Exception as e:
            if "rate" in str(e).lower() or "429" in str(e):
                _t.sleep(2 ** i)   # bounded backoff on rate-limit
                continue
            raise
    raise RuntimeError(f"giving up on {path} after {attempts} attempts")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/ingestion/test_concurrent_ingest.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/pipeline.py src/ingestion/classifier.py tests/ingestion/test_concurrent_ingest.py
git commit -m "feat(ingestion): bounded-concurrency document loader and concurrent classification"
```

---

### Task 12: Hybrid golden-set test harness (exact facts + LLM judge)

**Files:**
- Create: `tests/fixtures/README.md` (how to add a fixture)
- Create: `tests/fixtures/resolution/2026-R-12.expected.json` (example sidecar; the real `.pdf`/`.txt` provided by user)
- Create: `tests/ingestion/test_golden_set.py`
- Create: `src/evaluation/ingestion_judge.py`
- Test: itself (`test_golden_set.py`)

**Interfaces:**
- Consumes: `profile_document`, `SQLExtractor.extract_for_type`, `get_document_type`, an `Evaluator`-style LLM judge
- Produces:
  - `ingestion_judge.judge_extraction(source_text: str, extracted: dict, expected_notes: str, settings=None, client=None) -> dict` returning `{"score": 1-5, "complete": bool, "hallucinated": bool, "reasoning": str}`
  - `tests/ingestion/test_golden_set.py` parametrizes over every `*.expected.json` under `tests/fixtures/`: asserts hard facts exactly and judge score ≥ threshold.

- [ ] **Step 1: Write the expected.json contract + a failing test**

```json
// tests/fixtures/resolution/2026-R-12.expected.json
{
  "source_text_file": "2026-R-12.txt",
  "document_type": "resolution",
  "hard_facts": {
    "resolutions[0].resolution_number": "2026-R-12",
    "resolutions[0].amount": 40000.0,
    "resolutions[0].adopted_date": "2026-03-03"
  },
  "judge_notes": "Should capture the $40,000 award to the vendor and the council vote tally."
}
```

```python
# tests/ingestion/test_golden_set.py
import json, glob, os, pytest
from src.ingestion.registry import get_document_type
from src.ingestion.profiler import profile_document
from src.extraction.sql_extractor import SQLExtractor
# (helpers to build a ParsedDocument from the .txt fixture and a real TrackedAnthropic)

CASES = glob.glob("tests/fixtures/**/*.expected.json", recursive=True)

@pytest.mark.integration
@pytest.mark.parametrize("expected_path", CASES)
def test_golden_case(expected_path):
    spec = json.load(open(expected_path))
    src_dir = os.path.dirname(expected_path)
    source_text = open(os.path.join(src_dir, spec["source_text_file"])).read()
    parsed = _parsed_from_text(source_text)            # helper in the test module
    profile = profile_document(parsed, spec["source_text_file"])
    assert profile.document_type == spec["document_type"]          # HARD: classification
    doc_type = get_document_type(profile.document_type)
    extracted = SQLExtractor().extract_for_type(_chunks_from_text(source_text), doc_type)
    for path, want in spec["hard_facts"].items():                  # HARD: exact scalar facts
        assert _dig(extracted, path) == want, f"{path}: {_dig(extracted, path)} != {want}"
    from src.evaluation.ingestion_judge import judge_extraction    # SOFT: LLM judge
    verdict = judge_extraction(source_text, extracted, spec["judge_notes"])
    assert verdict["score"] >= 4 and not verdict["hallucinated"]
```

Include `_dig(obj, "resolutions[0].amount")`, `_parsed_from_text`, `_chunks_from_text` helpers in the test module (real, not placeholder): `_dig` parses `key[index].field` paths; the parsed/chunk helpers wrap the text in a single-page `ParsedDocument` / one `Chunk`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ingestion/test_golden_set.py -v`
Expected: FAIL (`src.evaluation.ingestion_judge` missing)

- [ ] **Step 3: Implement the judge (mirror `src/evaluation/evaluator.py`)**

```python
# src/evaluation/ingestion_judge.py
from __future__ import annotations
import json, logging
from typing import Optional
from src.config import Settings, get_settings
from src.llm.client import TrackedAnthropic

logger = logging.getLogger(__name__)

_PROMPT = """You audit structured data extracted from a city government document.
Given the SOURCE TEXT and the EXTRACTED JSON, judge the extraction.

Score 1-5 (5 = complete and faithful). Flag hallucination if any extracted value is
not supported by the source. Consider these expectations: {notes}

SOURCE TEXT:
---
{source}
---
EXTRACTED JSON:
{extracted}

Return ONLY JSON: {{"score": 1-5, "complete": true|false, "hallucinated": true|false, "reasoning": "..."}}"""

def judge_extraction(source_text, extracted, expected_notes="", settings=None, client=None):
    cfg = settings or get_settings()
    llm = client or TrackedAnthropic(cfg, call_site="eval.ingestion_judge")
    msg = llm.messages.create(
        model=cfg.synthesis_model, max_tokens=400,
        messages=[{"role": "user", "content": _PROMPT.format(
            notes=expected_notes, source=source_text[:6000],
            extracted=json.dumps(extracted)[:4000])}],
    )
    raw = msg.content[0].text.strip()
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("judge parse failed: %s", e)
        return {"score": 0, "complete": False, "hallucinated": True, "reasoning": "unparseable"}
```

Write `tests/fixtures/README.md`: "Drop one `<name>.txt` (or `.pdf`) per example into `tests/fixtures/<type>/`, plus a `<name>.expected.json` with `document_type`, `hard_facts` (exact scalar paths), and `judge_notes`."

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ingestion/test_golden_set.py -v -m integration`
Expected: PASS for the provided fixture(s). (Requires user-provided example docs + API key.)

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/ tests/ingestion/test_golden_set.py src/evaluation/ingestion_judge.py
git commit -m "test(ingestion): hybrid golden-set harness (exact facts + LLM judge)"
```

---

### Task 13: Re-ingest quarterly reports + regression gate

**Files:**
- Create: `scripts/reingest.py` (re-run ingestion over `docs/` through the new path)
- Test: run the existing query eval suite as the regression gate
- Modify: none (operational task)

**Interfaces:**
- Consumes: `IngestionPipeline` (Tasks 10/11), existing `EvaluationSuite`
- Produces: `scripts/reingest.py` CLI: clears + re-ingests so quarterly-report metadata becomes content-derived.

- [ ] **Step 1: Write the re-ingest script**

```python
# scripts/reingest.py
"""Re-ingest all documents through the agentic pipeline (content-derived metadata)."""
import logging, sys
from src.config import get_settings
from src.ingestion.pipeline import IngestionPipeline

logging.basicConfig(level=logging.INFO)

def main(docs_dir=None):
    cfg = get_settings()
    pipe = IngestionPipeline(cfg)
    pipe.initialize_stores()
    pipe.ingest_directory(docs_dir or cfg.docs_dir, skip_existing=False)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
```

- [ ] **Step 2: Re-ingest the existing quarterly reports**

Run: `python scripts/reingest.py docs/`
Expected: logs show each quarterly report profiled as `quarterly_report` with a department/period, ingested via the worker pool, no crashes.

- [ ] **Step 3: Run the regression gate (existing query eval suite)**

Run: `pytest tests/ -k eval -v` (or the project's eval entrypoint, e.g. `python -m src.evaluation.suite`)
Expected: query accuracy on quarterly-report questions is unchanged vs. the pre-migration baseline (no regression). Record the pass-rate.

- [ ] **Step 4: Spot-check resolutions end-to-end (manual)**

Place a few real resolution PDFs in a folder and run `python scripts/reingest.py <that folder>`; query the system for a known authorization amount and confirm a cited answer.

- [ ] **Step 5: Commit**

```bash
git add scripts/reingest.py
git commit -m "chore(ingestion): re-ingest script + regression gate for agentic migration"
```

---

## Self-Review

**Spec coverage check:**
- §4.1 new flow → Tasks 3 (profile), 2 (registry lookup), 5/6/9 (parameterized chunk/classify/extract), 10 (wiring). ✓
- §4.2 registry as Python/Pydantic → Task 2 + 7. ✓
- §4.3 content-derived metadata + re-ingest QRs → Tasks 4 + 13. ✓
- §4.4 quarantine unknown/low-confidence → Task 10 (`_is_quarantined`, vector-only). ✓
- §4.5 bounded-concurrency loader + concurrent classify_batch → Task 11. ✓
- §4.6 hybrid testing + regression gate → Tasks 12 + 13. ✓
- §4.7 cost guardrails (Haiku profiler, first-N-pages) → Task 1 (settings) + Task 3 (uses them). ✓
- Resolutions as first new type (SQL+graph+votes) → Tasks 7 + 8. ✓
- Optional category-hint → Task 3 (`category_hint` param) + Task 10 (threaded through `ingest_document`). ✓

**Placeholder scan:** No "TBD"/"implement later"; each code step shows real code. The only user-supplied artifacts are the golden-set example documents (Task 12), which is expected and documented in `tests/fixtures/README.md`.

**Type consistency:** `DocumentProfile`/`DocumentType`/`ChunkingHints` defined in Task 1 are consumed with matching fields in Tasks 2–10. `extract_for_type(chunks, doc_type)` (Task 9) is called with those exact args in Task 10. `insert_resolution_rows`/`insert_vote_rows` (Task 7) and `upsert_resolutions`/`upsert_votes`/`upsert_council_members` (Task 8) are referenced by those names in Task 10. `build_chunk_metadata(..., profile=..., needs_review=...)` (Task 4) matches its call in Task 10.

**Note on test layout:** create `tests/ingestion/__init__.py`, `tests/storage/__init__.py`, `tests/extraction/__init__.py` if the project requires package dirs; `pytest.ini` already configures discovery.
