# Ingestion Accuracy Guardrails — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop garbled/scanned documents from silently producing wrong structured data, and make each document's structured write all-or-nothing.

**Architecture:** Three guardrails plus two bug-fixes in the ingestion pipeline. A readability check flags gibberish text; garbled docs are re-read once by the Vision model; extracted fields are validated before any structured insert, and failures are withheld and recorded in a `review_flags` table (surfaced by a report script). Per-document structured writes run in a single transaction. The `votes.vote` column is widened and its inserts sanitized.

**Tech Stack:** Python 3.14, psycopg2, pydantic, pytest, Anthropic (Vision), PostgreSQL.

## Global Constraints

- Python source under `src/`, tests under `tests/`, mirrored package paths. `pythonpath = .` (pytest.ini).
- DB tests MUST be marked `@pytest.mark.integration`; all other tests run without a live database.
- Config is added to `src/config.py` `Settings` (pydantic-settings), read via `get_settings()`.
- Follow existing style: module-level functions for pipeline stages; `SQLStore.cursor()` context manager for DB access; `logger = logging.getLogger(__name__)`.
- Never make an LLM call in a unit test — monkeypatch parsers/extractors.
- Do NOT run the backfill (Task 8) without explicit user approval — it spends LLM budget.

---

### Task 1: Readability check to catch ASCII gibberish

**Files:**
- Create: `src/ingestion/quality.py`
- Modify: `src/config.py` (add `garble_readability_threshold`, `enable_vision_escalation`)
- Test: `tests/ingestion/test_quality.py`

**Interfaces:**
- Produces: `text_readability(text: str) -> float` (0.0–1.0), `is_garbled(text: str, settings: Settings | None = None) -> bool`
- Consumes: `src.config.get_settings`, `Settings`

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_quality.py
from src.ingestion.quality import text_readability, is_garbled

# Real captured text from a good resolution (readable)
_GOOD = (
    "WHEREAS, the City of Harrisburg City Council wishes to authorize the Mayor "
    "to enter into a lease agreement with the vendor; and WHEREAS the term shall "
    "be for a period of three years; NOW THEREFORE BE IT RESOLVED that the "
    "Council of the City of Harrisburg hereby approves the said agreement."
)
# Real captured gibberish from Res 19's bad OCR layer
_BAD = (
    "Yy Aavos AouoH feuoneN yd eddey nig No AYaId0S 1OU0H ssauisng euisis Bute "
    "eleg ZIG sscuoH jeuoQeUeyUy ASOVV ey Aq pezpes9e AyjeuoneEU ssautsng jo "
    "asayjoy NL oyTAaxyoo9 AUSIOAIUL YDI vossautiay suQuno2oy Ul YoMas.nod"
)

def test_good_text_scores_high():
    assert text_readability(_GOOD) > 0.5

def test_gibberish_scores_low():
    assert text_readability(_BAD) < 0.2

def test_is_garbled_true_for_gibberish():
    assert is_garbled(_BAD) is True

def test_is_garbled_false_for_good_text():
    assert is_garbled(_GOOD) is False

def test_empty_text_is_garbled():
    assert is_garbled("") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ingestion/test_quality.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ingestion.quality'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ingestion/quality.py
"""Readability scoring to detect garbled OCR text (gibberish made of ASCII letters).

The detector's non-ASCII garble check misses bad embedded OCR layers whose text is
ordinary letters in nonsense order. This scores text by how much it looks like real
English government prose, primarily via stopword hit-rate.
"""
from __future__ import annotations

import re
from typing import Optional

from src.config import Settings, get_settings

# High-frequency English + City-government words. Genuine text hits many of these;
# gibberish hits almost none.
_STOPWORDS = frozenset({
    "the", "of", "and", "to", "a", "in", "for", "is", "on", "that", "by", "this",
    "with", "be", "as", "at", "or", "an", "shall", "hereby", "whereas", "resolved",
    "city", "council", "mayor", "department", "agreement", "authorize", "authorized",
    "resolution", "ordinance", "section", "meeting", "member", "vote", "year",
})

_WORD_RE = re.compile(r"[A-Za-z]+")


def text_readability(text: str) -> float:
    """Fraction of word tokens that are real English words, blending a stopword
    hit-rate with a structural (vowel-bearing, plausible-length) check. 0.0–1.0."""
    if not text or not text.strip():
        return 0.0
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return 0.0

    stop_hits = sum(1 for t in tokens if t in _STOPWORDS)
    stop_rate = stop_hits / len(tokens)

    # Structural plausibility: real words carry a vowel and are 2–15 chars.
    def _plausible(t: str) -> bool:
        return 2 <= len(t) <= 15 and any(v in t for v in "aeiou")
    struct_rate = sum(1 for t in tokens if _plausible(t)) / len(tokens)

    # Stopword presence is the strong signal; weight it heavily. Real prose has a
    # stop_rate well above 0.15; gibberish is near zero.
    return min(1.0, stop_rate * 3.0) * 0.7 + struct_rate * 0.3


def is_garbled(text: str, settings: Optional[Settings] = None) -> bool:
    """True when text reads as gibberish (below the configured readability floor)."""
    cfg = settings or get_settings()
    return text_readability(text) < cfg.garble_readability_threshold
```

Add to `src/config.py` `Settings` (after the existing `garbled_ratio_threshold` line, ~line 70):

```python
    # Readability gate: docs whose parsed text scores below this are treated as
    # garbled (bad OCR) and re-read with the Vision LLM. See src/ingestion/quality.py.
    garble_readability_threshold: float = 0.35
    enable_vision_escalation: bool = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/ingestion/test_quality.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/quality.py src/config.py tests/ingestion/test_quality.py
git commit -m "feat(ingest): readability check to detect ASCII gibberish OCR"
```

---

### Task 2: Field validation gate

**Files:**
- Create: `src/ingestion/validation.py`
- Test: `tests/ingestion/test_validation.py`

**Interfaces:**
- Produces: `validate_extraction(doc_type_name: str, extracted: dict, profile=None) -> list[str]` — returns a list of human-readable problem strings; empty list means valid.
- Consumes: nothing outside stdlib.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_validation.py
from src.ingestion.validation import validate_extraction

def _res(num, votes=3):
    return {
        "resolutions": [{"resolution_number": num, "vendor": "Acme", "title": "x"}],
        "votes": [{"resolution_number": num, "council_member": f"M{i}", "vote": "yes"}
                  for i in range(votes)],
    }

def test_valid_resolution_passes():
    assert validate_extraction("resolution", _res("21-2026")) == []

def test_number_equal_to_year_rejected():
    problems = validate_extraction("resolution", _res("2026-2026"))
    assert any("2026-2026" in p for p in problems)

def test_malformed_number_rejected():
    problems = validate_extraction("resolution", _res("../-2026"))
    assert problems  # non-empty

def test_missing_resolution_row_rejected():
    problems = validate_extraction("resolution", {"resolutions": [], "votes": []})
    assert problems

def test_implausible_vote_count_rejected():
    problems = validate_extraction("resolution", _res("21-2026", votes=40))
    assert any("vote" in p.lower() for p in problems)

def test_unknown_type_is_noop():
    assert validate_extraction("mystery", {"anything": []}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ingestion/test_validation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ingestion.validation'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ingestion/validation.py
"""Post-extraction sanity checks. Returns a list of problems; empty == valid.

Validators are keyed by document type so a bad extraction (e.g. a resolution number
read as the year off a garbled scan) is caught before it reaches the structured tables.
"""
from __future__ import annotations

import re

_RES_NUM_RE = re.compile(r"^\d{1,4}-(\d{4})$")
_MAX_VOTES = 15


def _validate_resolution(extracted: dict) -> list[str]:
    problems: list[str] = []
    rows = extracted.get("resolutions") or []
    if not rows:
        return ["no resolution row extracted"]
    for r in rows:
        num = str(r.get("resolution_number") or "").strip()
        m = _RES_NUM_RE.match(num)
        if not m:
            problems.append(f"resolution_number {num!r} is not of the form N-YYYY")
            continue
        seq, year = num.split("-")
        if seq == year:
            problems.append(f"resolution_number {num!r} has sequence equal to year (impossible)")
    votes = extracted.get("votes") or []
    if len(votes) > _MAX_VOTES:
        problems.append(f"implausible vote count: {len(votes)}")
    return problems


_VALIDATORS = {
    "resolution": _validate_resolution,
}


def validate_extraction(doc_type_name: str, extracted: dict, profile=None) -> list[str]:
    """Return a list of problem strings for the extracted data; [] means valid.
    Unknown document types have no validator and always pass ([])."""
    validator = _VALIDATORS.get(doc_type_name)
    if validator is None:
        return []
    return validator(extracted or {})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/ingestion/test_validation.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/validation.py tests/ingestion/test_validation.py
git commit -m "feat(ingest): pre-write validation gate for extracted fields"
```

---

### Task 3: Widen and sanitize the vote field

**Files:**
- Modify: `sql/schema.sql:214` (votes.vote type)
- Create: `sql/migrate_2026_07_09_guardrails.sql`
- Modify: `src/storage/sql_store.py` (`insert_vote_rows`, ~line 198)
- Test: `tests/storage/test_vote_sanitize.py`

**Interfaces:**
- Produces: `sanitize_vote(value) -> str | None` (module-level in `sql_store.py`), used by `insert_vote_rows`.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_vote_sanitize.py
from src.storage.sql_store import sanitize_vote

def test_normal_vote_unchanged():
    assert sanitize_vote("yes") == "yes"

def test_overlong_vote_truncated_to_50():
    v = sanitize_vote("affirmative with a very long qualifying explanation attached here")
    assert v is not None and len(v) <= 50

def test_none_stays_none():
    assert sanitize_vote(None) is None

def test_whitespace_trimmed():
    assert sanitize_vote("  yes  ") == "yes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/storage/test_vote_sanitize.py -v`
Expected: FAIL — `ImportError: cannot import name 'sanitize_vote'`

- [ ] **Step 3: Write minimal implementation**

In `src/storage/sql_store.py`, add a module-level helper near the top (after `_dumps`, ~line 36):

```python
_VOTE_MAXLEN = 50


def sanitize_vote(value) -> str | None:
    """Trim and cap a vote value so an over-long/garbled value can never crash the
    votes insert (votes.vote is VARCHAR(50))."""
    if value is None:
        return None
    s = str(value).strip()
    return s[:_VOTE_MAXLEN] if s else None
```

Change `insert_vote_rows` (~line 204) to use it:

```python
        with self.cursor() as cur:
            for r in rows:
                cur.execute(sql, (
                    r.get("resolution_number"), r.get("council_member"),
                    sanitize_vote(r.get("vote")), source_chunk_id, source_file,
                ))
```

Edit `sql/schema.sql` line 214, change `vote VARCHAR(10)` to `vote VARCHAR(50)`.

Create `sql/migrate_2026_07_09_guardrails.sql`:

```sql
-- Guardrails migration (2026-07-09). Idempotent; safe to re-run.
ALTER TABLE votes ALTER COLUMN vote TYPE VARCHAR(50);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/storage/test_vote_sanitize.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/storage/sql_store.py sql/schema.sql sql/migrate_2026_07_09_guardrails.sql tests/storage/test_vote_sanitize.py
git commit -m "fix(ingest): widen votes.vote to varchar(50) and sanitize vote values"
```

---

### Task 4: Atomic per-document structured write

**Files:**
- Modify: `src/storage/sql_store.py` (`__init__` ~line 39, `cursor` ~line 68; add `transaction`)
- Modify: `src/ingestion/pipeline.py` (`_write_typed_data` ~line 343)
- Test: `tests/storage/test_transaction.py`

**Interfaces:**
- Produces: `SQLStore.transaction()` context manager. Inside it, `cursor()` calls defer their commit and reuse the live connection; a single commit happens on clean exit, a single rollback on any exception.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_transaction.py
from src.storage.sql_store import SQLStore

class _FakeCur:
    def execute(self, *a, **k): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

class _FakeConn:
    def __init__(self): self.commits = 0; self.rollbacks = 0; self.closed = False
    def cursor(self): return _FakeCur()
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1

def _store(conn):
    s = SQLStore.__new__(SQLStore)
    s._conn = conn
    s._in_txn = False
    return s

def test_transaction_commits_once_for_multiple_cursors():
    conn = _FakeConn(); s = _store(conn)
    with s.transaction():
        with s.cursor() as c: c.execute("A")
        with s.cursor() as c: c.execute("B")
    assert conn.commits == 1        # one commit for the whole block
    assert conn.rollbacks == 0

def test_transaction_rolls_back_all_on_error():
    conn = _FakeConn(); s = _store(conn)
    try:
        with s.transaction():
            with s.cursor() as c: c.execute("A")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conn.commits == 0
    assert conn.rollbacks >= 1

def test_cursor_without_transaction_commits_itself():
    conn = _FakeConn(); s = _store(conn)
    with s.cursor() as c: c.execute("A")
    assert conn.commits == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/storage/test_transaction.py -v`
Expected: FAIL — `AttributeError: 'SQLStore' object has no attribute 'transaction'` (and `_in_txn` handling absent)

- [ ] **Step 3: Write minimal implementation**

In `src/storage/sql_store.py` `__init__` (~line 39-41), add the flag:

```python
    def __init__(self, settings: Optional[Settings] = None):
        self.cfg = settings or get_settings()
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._in_txn = False
```

Replace `cursor` (~line 68-79) and add `transaction` right after it:

```python
    @contextmanager
    def cursor(self):
        # Inside a transaction() block, reuse the live connection directly and defer
        # the commit to transaction(); calling _get_live_conn (which pings + rolls
        # back) mid-transaction would abort the in-progress work.
        if self._in_txn and self._conn and not self._conn.closed:
            conn = self._conn
        else:
            conn = self._get_live_conn()
        cur = conn.cursor()
        try:
            yield cur
            if not self._in_txn:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    @contextmanager
    def transaction(self):
        """Group multiple insert calls into one atomic commit. cursor() calls inside
        this block defer their commit; the whole block commits once on success or
        rolls back entirely on any exception."""
        conn = self._get_live_conn()
        self._in_txn = True
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._in_txn = False
```

In `src/ingestion/pipeline.py` `_write_typed_data` (~line 350), wrap the SQL inserts (NOT the graph block) in a transaction. Change the SQL section to:

```python
        # SQL — all of one document's structured rows commit together (atomic), so a
        # failure can never leave a half-ingested doc (e.g. a resolution with no votes).
        with self.sql_store.transaction():
            if "resolutions" in doc_type.sql_targets and extracted.get("resolutions"):
                self.sql_store.insert_resolution_rows(extracted["resolutions"], chunk_id, source_file)
            if "votes" in doc_type.sql_targets and extracted.get("votes"):
                self.sql_store.insert_vote_rows(extracted["votes"], chunk_id, source_file)
            if "meetings" in doc_type.sql_targets and extracted.get("meetings"):
                self.sql_store.insert_meeting_rows(extracted["meetings"], chunk_id, source_file)
            if "meeting_actions" in doc_type.sql_targets and extracted.get("meeting_actions"):
                mdate = (extracted.get("meetings") or [{}])[0].get("meeting_date")
                for a in extracted["meeting_actions"]:
                    a.setdefault("meeting_date", mdate)
                self.sql_store.insert_meeting_action_rows(extracted["meeting_actions"], chunk_id, source_file)
            if "legislation" in doc_type.sql_targets and extracted.get("legislation"):
                self.sql_store.insert_legislation_rows(extracted["legislation"], chunk_id, source_file)
            if "appropriations" in doc_type.sql_targets and extracted.get("appropriations"):
                self.sql_store.insert_appropriation_rows(extracted["appropriations"], chunk_id, source_file)
```

(Leave the name-normalization loop above it and the graph `try/except` block below it unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/storage/test_transaction.py tests/ingestion/test_pipeline_typed_write.py -v`
Expected: PASS (existing typed-write test still passes; 3 new pass)

- [ ] **Step 5: Commit**

```bash
git add src/storage/sql_store.py src/ingestion/pipeline.py tests/storage/test_transaction.py
git commit -m "fix(ingest): atomic per-document structured writes"
```

---

### Task 5: review_flags table and store methods

**Files:**
- Modify: `sql/schema.sql` (add `review_flags` table near votes, ~line 218)
- Modify: `sql/migrate_2026_07_09_guardrails.sql` (append CREATE TABLE)
- Modify: `src/storage/sql_store.py` (add `insert_review_flag`, `get_unresolved_review_flags`)
- Test: `tests/storage/test_review_flags.py` (marked integration)

**Interfaces:**
- Produces: `SQLStore.insert_review_flag(source_file: str, stage: str, reason: str, detail: str = "") -> None`; `SQLStore.get_unresolved_review_flags() -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_review_flags.py
import pytest
from src.config import get_settings
from src.storage.sql_store import SQLStore

@pytest.mark.integration
def test_insert_and_read_review_flag():
    s = SQLStore(get_settings()); s.connect()
    try:
        s.insert_review_flag("z-test.pdf", "validate", "bad number", "2026-2026")
        flags = s.get_unresolved_review_flags()
        assert any(f["source_file"] == "z-test.pdf" and f["reason"] == "bad number"
                   for f in flags)
    finally:
        with s.cursor() as cur:
            cur.execute("DELETE FROM review_flags WHERE source_file = 'z-test.pdf'")
        s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/storage/test_review_flags.py -v -m integration`
Expected: FAIL — `AttributeError: 'SQLStore' object has no attribute 'insert_review_flag'` (or missing-table error)

- [ ] **Step 3: Write minimal implementation**

Add to `sql/schema.sql` after the votes index block (~line 222):

```sql
-- Documents/rows withheld from structured tables pending human review.
CREATE TABLE IF NOT EXISTS review_flags (
    id           SERIAL PRIMARY KEY,
    source_file  VARCHAR(255) NOT NULL,
    stage        VARCHAR(20)  NOT NULL,   -- parse | classify | validate
    reason       TEXT         NOT NULL,
    detail       TEXT,
    resolved     BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_review_flags_unresolved ON review_flags(resolved);
```

Append the same `CREATE TABLE IF NOT EXISTS review_flags (...)` and index to `sql/migrate_2026_07_09_guardrails.sql`.

Add to `src/storage/sql_store.py` (after `record_document`, ~line 182):

```python
    def insert_review_flag(self, source_file: str, stage: str, reason: str, detail: str = "") -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO review_flags (source_file, stage, reason, detail) "
                "VALUES (%s, %s, %s, %s)",
                (source_file, stage, reason, detail),
            )

    def get_unresolved_review_flags(self) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT source_file, stage, reason, detail, created_at "
                "FROM review_flags WHERE resolved = FALSE ORDER BY created_at DESC"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
```

(Note: `SQLStore.cursor()` returns a plain psycopg2 cursor here; the `zip(cols, row)` build matches the non-RealDict path used elsewhere. If this store is configured with RealDictCursor, `cur.fetchall()` already returns dicts — keep the explicit build for safety since rows are tuples in the default factory.)

- [ ] **Step 4: Run test to verify it passes**

First apply the migration to the live DB:
Run: `psql "$DATABASE_URL" -f sql/migrate_2026_07_09_guardrails.sql`
Then: `python -m pytest tests/storage/test_review_flags.py -v -m integration`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add sql/schema.sql sql/migrate_2026_07_09_guardrails.sql src/storage/sql_store.py tests/storage/test_review_flags.py
git commit -m "feat(ingest): review_flags table and store accessors"
```

---

### Task 6: Wire guardrails into the pipeline

**Files:**
- Modify: `src/ingestion/pipeline.py` (`_parse_with_fallback` ~line 228, `ingest_document` ~line 114, `_store_chunks` ~line 270)
- Test: `tests/ingestion/test_guardrail_wiring.py`

**Interfaces:**
- Consumes: `quality.is_garbled`, `validation.validate_extraction`, `SQLStore.insert_review_flag`, `vision_parser.parse`.
- Produces: pipeline behavior — garbled parse → Vision re-read; invalid/unclassified/still-garbled → review flag + structured write withheld.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_guardrail_wiring.py
from pathlib import Path
from src.ingestion import pipeline as P
from src.models import ParsedDocument, ParsedElement

def _pipe():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    from src.config import get_settings
    pipe.cfg = get_settings()
    return pipe

def _doc(parser, text):
    el = ParsedElement(text=text, element_type="NarrativeText", page_number=1)
    return ParsedDocument(source_file="f.pdf", parser_used=parser, elements=[el], total_pages=1)

_GIB = "Yy Aavos AouoH feuoneN yd eddey nig No AYaId0S ssauisng euisis Bute eleg"
_GOOD = ("WHEREAS the City of Harrisburg City Council shall authorize the Mayor to "
         "enter into the agreement and be it resolved by the council of the city.")

def test_garbled_clean_text_escalates_to_vision(monkeypatch):
    # clean_text path returns gibberish -> is_garbled -> re-read with Vision
    monkeypatch.setattr(P.unstructured_parser, "parse", lambda p: _doc("unstructured", _GIB))
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: _doc("vision_llm", _GOOD))
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "clean_text_pdf")
    assert out.parser_used == "vision_llm"

def test_readable_clean_text_stays(monkeypatch):
    monkeypatch.setattr(P.unstructured_parser, "parse", lambda p: _doc("unstructured", _GOOD))
    monkeypatch.setattr(P.vision_parser, "parse",
                        lambda p, c: (_ for _ in ()).throw(AssertionError("no vision")))
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "clean_text_pdf")
    assert out.parser_used == "unstructured"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ingestion/test_guardrail_wiring.py -v`
Expected: FAIL — `AssertionError` (Vision not invoked; escalation not wired)

- [ ] **Step 3: Write minimal implementation**

In `src/ingestion/pipeline.py`, add imports (top, with the other `from src.ingestion import` line ~line 28):

```python
from src.ingestion import chunker, classifier, detector, metadata, quality, validation
```

Add a helper to assemble parsed text and extend `_parse_with_fallback` to escalate. Replace the `clean_text_pdf`/`word_doc` branch and add the garble check at the end of `_parse_with_fallback` (~line 241-249):

```python
        if doc_kind in ("clean_text_pdf", "word_doc"):
            try:
                parsed = unstructured_parser.parse(path)
            except ParseQualityError as e:
                logger.warning("Unstructured quality check failed: %s — retrying with Vision LLM", e)
                return vision_parser.parse(path, self.cfg)
            return self._escalate_if_garbled(path, parsed)

        raise ValueError(f"Unsupported document kind: {doc_kind}")

    @staticmethod
    def _assemble_text(parsed) -> str:
        return "\n".join(e.text for e in parsed.elements if getattr(e, "text", None))

    def _escalate_if_garbled(self, path: Path, parsed):
        """If parsed text reads as gibberish (bad OCR layer), re-read once with Vision."""
        if not self.cfg.enable_vision_escalation or parsed.parser_used == "vision_llm":
            return parsed
        if quality.is_garbled(self._assemble_text(parsed), self.cfg):
            logger.info("%s → parsed text is garbled — re-reading with Vision LLM", path.name)
            return vision_parser.parse(path, self.cfg)
        return parsed
```

Also apply escalation on the Tesseract-OK branch (~line 234). Change:

```python
                if tesseract_parser.ocr_quality_ok(parsed, self.cfg):
                    return self._escalate_if_garbled(path, parsed)
```

In `_store_chunks`, guard the typed-write path with validation (~line 339, the "Other known types" section). Replace:

```python
        extracted = self.sql_extractor.extract_for_type(chunks, doc_type, profile=profile)
        if extracted:
            self._write_typed_data(extracted, chunks, source_file, doc_type)
```

with:

```python
        extracted = self.sql_extractor.extract_for_type(chunks, doc_type, profile=profile)
        problems = validation.validate_extraction(doc_type.name, extracted or {}, profile)
        if problems:
            logger.warning("  → %s failed validation, withholding structured write: %s",
                           source_file, "; ".join(problems))
            self.sql_store.insert_review_flag(
                source_file, "validate", "; ".join(problems),
                str((extracted or {}).get(doc_type.sql_targets[0], "")),
            )
            return
        if extracted:
            self._write_typed_data(extracted, chunks, source_file, doc_type)
```

In `ingest_document`, when a document is quarantined, record a flag (~line 147, right after `quarantined = self._is_quarantined(profile)`):

```python
        if quarantined:
            self.sql_store.insert_review_flag(
                path.name, "classify",
                f"quarantined: type={profile.document_type} confidence={profile.confidence:.2f}",
                profile.document_type or "",
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/ingestion/test_guardrail_wiring.py tests/ingestion/test_parse_routing.py -v`
Expected: PASS (existing parse-routing tests still pass; 2 new pass)

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/pipeline.py tests/ingestion/test_guardrail_wiring.py
git commit -m "feat(ingest): wire garble escalation + validation gate + review flags"
```

---

### Task 7: Review report script

**Files:**
- Create: `scripts/review_report.py`
- Test: none (thin operator script; logic covered by Task 5)

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Print documents withheld from the structured tables pending review.
Operator-run (needs live DB). Usage: python3 scripts/review_report.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.storage.sql_store import SQLStore


def main():
    s = SQLStore(get_settings()); s.connect()
    try:
        flags = s.get_unresolved_review_flags()
    finally:
        s.close()

    if not flags:
        print("No documents pending review. ✅")
        return

    by_reason: dict[str, list[dict]] = {}
    for f in flags:
        by_reason.setdefault(f["stage"], []).append(f)

    print(f"{len(flags)} document(s) need review:\n")
    for stage, items in sorted(by_reason.items()):
        print(f"== {stage} ==")
        for f in items:
            detail = (f.get("detail") or "").strip().replace("\n", " ")
            print(f"  • {f['source_file']}")
            print(f"      {f['reason']}")
            if detail:
                print(f"      got: {detail[:160]}")
        print()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it runs**

Run: `python3 scripts/review_report.py`
Expected: prints "No documents pending review. ✅" (before backfill) or a list, with no traceback.

- [ ] **Step 3: Commit**

```bash
git add scripts/review_report.py
git commit -m "feat(ingest): review_report script listing docs pending review"
```

---

### Task 8: Backfill the four affected documents (REQUIRES USER APPROVAL — spends LLM budget)

**Files:**
- Uses: `scripts/ingest.py --file ... --reingest`
- No code changes.

**Do not run without explicit user approval.** Estimated spend ~$0.20 (Vision on the three scans + Res 8).

- [ ] **Step 1: Confirm migration is applied**

Run: `psql "$DATABASE_URL" -f sql/migrate_2026_07_09_guardrails.sql`
Expected: `ALTER TABLE` / `CREATE TABLE` (no error; idempotent).

- [ ] **Step 2: Re-ingest the four documents**

Re-ingest is idempotent (clears prior rows by `source_file`), which also removes the bogus `4-2026` / `2026-2026` rows.

```bash
cd /Users/leenadudi/council.knowledge.base
for n in "Resolution 8-2026" "Resolution 19-2026 - Katherine O'Flaherty Appointment to HARB.pdf" "Resolution 20-2026" "Resolution 21-2026"; do
  f=$(ls docs/ | grep -F "$n" | grep -v '(1)' | head -1)
  python scripts/ingest.py --file "docs/$f" --reingest
done
```

- [ ] **Step 3: Verify the repair**

```bash
python3 - <<'PY'
import sys; sys.path.insert(0,'.')
from src.config import get_settings
from src.storage.sql_store import SQLStore
s = SQLStore(get_settings()); s.connect()
with s.cursor() as cur:
    cur.execute("SELECT resolution_number, (SELECT count(*) FROM votes v WHERE v.resolution_number=r.resolution_number) nv FROM resolutions r WHERE source_file ILIKE '%Resolution 8-%' OR source_file ILIKE '%Resolution 20%' OR source_file ILIKE '%Resolution 21%' ORDER BY resolution_number")
    for row in cur.fetchall(): print(row)
    cur.execute("SELECT count(*) FROM resolutions WHERE resolution_number IN ('2026-2026','4-2026')")
    print("bogus rows remaining (expect 0):", cur.fetchone())
s.close()
PY
```
Expected: Res 8 has votes; Res 20/21 show valid `N-2026` numbers (or appear in `review_report.py` if Vision still couldn't read them); zero bogus rows.

- [ ] **Step 4: Run the review report**

Run: `python3 scripts/review_report.py`
Expected: Res 19 (and any doc Vision still couldn't parse) listed for review, rather than silently mis-ingested.

---

## Self-Review

**Spec coverage:**
- Guardrail 1 (readability) → Task 1 ✓
- Guardrail 2 (Vision escalation) → Task 6 (`_escalate_if_garbled`) ✓
- Guardrail 3 (validate before write, else review flag) → Task 2 + Task 6 wiring ✓
- Bug-fix A (atomic writes) → Task 4 ✓
- Bug-fix B (widen + sanitize vote) → Task 3 ✓
- review_flags table + report → Task 5 + Task 7 ✓
- Config additions → Task 1 ✓
- Backfill → Task 8 ✓

**Placeholder scan:** none — all steps carry real code/commands.

**Type consistency:** `is_garbled(text, settings)`, `validate_extraction(doc_type_name, extracted, profile)`, `sanitize_vote(value)`, `insert_review_flag(source_file, stage, reason, detail)`, `get_unresolved_review_flags()`, `SQLStore.transaction()`, `_escalate_if_garbled(path, parsed)`, `_assemble_text(parsed)` — all used consistently across tasks.
