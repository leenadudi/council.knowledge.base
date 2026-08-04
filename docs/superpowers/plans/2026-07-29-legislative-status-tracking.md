# Legislative Status Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the lifecycle status model from the 2026-07-29 design spec: a `legislative_items` dimension, a deterministic event normalizer, a canonical `legislative_events` trail, derived-status SQL views, a backfill script, and ingest/classifier integration.

**Architecture:** `meeting_actions` free text is normalized by pure Python keyword rules into canonical events against an auto-discovered `legislative_items` dimension; document-derived end states (signed/vetoed) come from `resolutions`/`legislation` rows. Current stage is a max-rank SQL view — nothing stored that can drift. All derived data is recomputable from raw; a full-rebuild backfill script is idempotent by construction.

**Tech Stack:** Python 3.11+, psycopg2, PostgreSQL 16, pytest.

## Global Constraints

- **No LLM spend.** Deterministic SQL/Python only; no ingestion, extraction, or Ask-test calls (owner's funds rule).
- **City-agnostic.** No city-specific numbers, committees, or names hardcoded in src/ or sql/ (test fixtures may use observed Harrisburg strings).
- **Additive & non-destructive.** Raw `item_type`/`action`/`status` strings stay untouched as audit trail; new columns nullable; existing tables only gain columns.
- **Never touch production.** DB-touching verification uses `SCRATCH_DATABASE_URL`; applying the migration to the real DB is a separate human-run step.
- **Graceful degradation.** If the migration hasn't been applied, ingest must still work — event emission is gated on a `to_regclass('legislative_items')` probe.
- Migrations are `sql/migrate_YYYY_MM_DD_*.sql`, idempotent (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`, guarded `ADD COLUMN`); the same DDL is appended to `sql/schema.sql` for fresh installs (precedent: `review_flags`).
- Live-DB tests are `@pytest.mark.integration`.
- Spec of record: `docs/superpowers/specs/2026-07-29-legislative-status-tracking-design.md`.

## File Structure

- `src/ingestion/legislative_events.py` — NEW: pure normalizer (`item_kind`, `item_key`, `parse_events`, `doc_status_events`, `LegislativeEvent`).
- `sql/migrate_2026_07_29_legislative_status.sql` — NEW: tables, FK columns, indexes, two views.
- `sql/schema.sql` — MODIFY: append the same DDL.
- `src/storage/sql_store.py` — MODIFY: `resolve_legislative_item`, `_insert_legislative_events`, tracking probe; wire into `insert_meeting_action_rows`, `insert_resolution_rows`, `insert_legislation_rows`, `insert_vote_rows`, `delete_structured_rows`.
- `scripts/backfill_legislative_events.py` — NEW: pure `derive()` + transactional `apply()`, `--dry-run`.
- `src/query/classifier.py` — MODIFY: add the two views to the SQL schema reference.
- Tests: `tests/ingestion/test_legislative_events.py`, `tests/storage/test_legislative_tracking.py`, `tests/query/test_classifier_prompt.py`, `tests/storage/test_legislative_status_integration.py`.

---

### Task 1: Normalizer module (`legislative_events.py`)

**Files:**
- Create: `src/ingestion/legislative_events.py`
- Test: `tests/ingestion/test_legislative_events.py`

**Interfaces (produces — later tasks import exactly these):**
- `LegislativeEvent` frozen dataclass: `event: str`, `committee: str = ""`, `ayes: int | None = None`, `nays: int | None = None`, `note: str = ""`
- `item_kind(item_type: str | None, item_number: str | None = None) -> str | None` → `'resolution' | 'ordinance' | None`
- `item_key(item_number: str | None) -> str` → canonical lowercase key, `""` if untrackable
- `parse_events(item_type: str | None, action: str | None) -> list[LegislativeEvent]`
- `doc_status_events(status: str | None) -> list[str]` → event names from a document's own `status` field
- `EVENT_VOCABULARY: frozenset[str]`

Clause policy (refines spec §3.2, consistent with its `note` column): a clause matching no rule attaches as `note` to the preceding event of the same action string; only when the **whole string** yields zero events does the caller flag it (`parse_events` returns `[]`).

- [ ] **Step 1: Write the failing tests** — table-driven over the observed vocabulary plus adversarial cases:

```python
"""Unit tests for the deterministic legislative-event normalizer.

Fixture strings are real observed values from meeting_actions/legislation —
the normalizer itself must stay city-agnostic (keyword rules only).
"""
import pytest

from src.ingestion.legislative_events import (
    EVENT_VOCABULARY, LegislativeEvent, doc_status_events, item_key, item_kind,
    parse_events,
)


# ---------------------------------------------------------------- item_kind
@pytest.mark.parametrize("item_type,item_number,expected", [
    ("resolution", "20-2026", "resolution"),
    ("Resolution", "20-2026", "resolution"),
    ("ordinance", "1-2026", "ordinance"),
    ("Ordinance", "1-2026", "ordinance"),
    ("Ordinance for Final Passage", "3-2026", "ordinance"),
    ("Ordinance for Amendment", "3-2026", "ordinance"),
    ("", "Bill 3-2026", "ordinance"),          # number prefix rescues empty type
    ("Committee Report", "Administration", None),
    ("Minutes Approval", "", None),
    ("Approval of Minutes", "", None),
    ("Minutes", "", None),
    ("New Business", "", None),
    (None, None, None),
])
def test_item_kind(item_type, item_number, expected):
    assert item_kind(item_type, item_number) == expected


# ----------------------------------------------------------------- item_key
@pytest.mark.parametrize("raw,expected", [
    ("Bill 3-2026", "3-2026"),
    ("bill 3-2026", "3-2026"),
    ("3-2026", "3-2026"),
    ("Res. 20-2026", "20-2026"),
    ("Resolution No. 20-2026", "20-2026"),
    ("No. 7-2026", "7-2026"),
    ("#7-2026", "7-2026"),
    ("  1-2026  ", "1-2026"),
    ("", ""),
    (None, ""),
])
def test_item_key(raw, expected):
    assert item_key(raw) == expected


def test_item_key_identity_across_prefix_variants():
    assert item_key("Bill 3-2026") == item_key("3-2026")


# -------------------------------------------------------------- parse_events
def _names(events):
    return [e.event for e in events]


@pytest.mark.parametrize("action,expected", [
    ("read into record", ["introduced"]),
    ("Read into record for amendment", ["introduced"]),
    ("first reading", ["first_reading"]),
    ("referred to committee", ["referred_to_committee"]),
    ("Referred to Administration Committee", ["referred_to_committee"]),
    ("Moved to committee", ["referred_to_committee"]),
    ("Moved into Budget & Finance Committee", ["referred_to_committee"]),
    ("Reported out", ["reported_out"]),
    ("final passage", ["passed"]),
    ("Passed 7-0", ["passed"]),
    ("approved", ["passed"]),
    ("tabled", ["tabled"]),
    ("withdrawn", ["withdrawn"]),
    ("defeated", ["defeated"]),
    ("motion failed", ["defeated"]),
    ("amendment adopted 7-0", ["amended"]),
    ("amendment passed 6-0", ["amended"]),
])
def test_single_clause_events(action, expected):
    assert _names(parse_events("resolution", action)) == expected


def test_multi_clause_splits_semicolon():
    evs = parse_events("ordinance", "first reading; referred to committee")
    assert _names(evs) == ["first_reading", "referred_to_committee"]


def test_multi_clause_splits_em_dash():
    evs = parse_events("Ordinance", "First Reading — referred to Administration Committee")
    assert _names(evs) == ["first_reading", "referred_to_committee"]
    assert evs[1].committee == "Administration Committee"


def test_committee_extracted_from_referral():
    evs = parse_events("Resolution", "Referred to Parks, Recreation & Enrichment Committee")
    assert evs[0].committee == "Parks, Recreation & Enrichment Committee"


def test_tally_parsed():
    evs = parse_events("Resolution", "Passed 6-1, with Councilwoman Davis voting in the negative")
    assert _names(evs) == ["passed"]
    assert (evs[0].ayes, evs[0].nays) == (6, 1)
    assert "voting in the negative" in evs[0].note


def test_dissent_clause_is_not_defeated():
    # "voting in the negative" must never read as a defeat when ayes > nays
    evs = parse_events(
        "Resolution",
        "Passed 4-3; Vice President Jones, Councilwoman Rawls, and President Hill voted in the negative",
    )
    assert _names(evs) == ["passed"]
    assert (evs[0].ayes, evs[0].nays) == (4, 3)


def test_did_not_pass_is_defeated():
    assert _names(parse_events("resolution", "motion did not pass")) == ["defeated"]


def test_passed_as_amended_emits_both():
    evs = parse_events("Ordinance for Final Passage", "Passed 7-0 as amended")
    assert _names(evs) == ["passed", "amended"]
    assert (evs[0].ayes, evs[0].nays) == (7, 0)


def test_recusal_clause_becomes_note_not_flag():
    evs = parse_events(
        "Ordinance for Final Passage",
        "Passed 6-0; Councilwoman Green recused herself due to potential conflict of interest",
    )
    assert _names(evs) == ["passed"]
    assert "recused" in evs[0].note


def test_final_passage_with_extra_reading_clause():
    evs = parse_events("Ordinance", "Passed 7-0 (Final Passage); also read for Amendment")
    assert evs[0].event == "passed"


def test_unmapped_string_returns_empty():
    assert parse_events("Resolution", "Announced for March 31, 2026 at 5:30 PM") == []


@pytest.mark.parametrize("action", [None, "", "None"])
def test_null_like_actions_yield_nothing(action):
    assert parse_events("Ordinance", action) == []


def test_long_amendment_narrative_keeps_note():
    action = ("Read into record for amendment; amendment added language to subsection "
              "2-301.5.C prohibiting Mayor from appointing interim or temporary department "
              "head and prohibiting Controller from approving compensation for such "
              "appointment; amendment passed 7-0")
    evs = parse_events("Ordinance for Amendment", action)
    assert _names(evs) == ["introduced", "amended"]
    assert "added language" in evs[0].note      # unmatched clause → note on prior event
    assert (evs[1].ayes, evs[1].nays) == (7, 0)


def test_all_events_in_vocabulary():
    samples = [
        "read into record", "first reading", "referred to committee", "Reported out",
        "Passed 7-0", "tabled", "withdrawn", "defeated", "amendment adopted 7-0",
    ]
    for s in samples:
        for e in parse_events("resolution", s):
            assert e.event in EVENT_VOCABULARY


# ---------------------------------------------------------- doc_status_events
@pytest.mark.parametrize("status,expected", [
    ("Passed", ["passed"]),
    ("Adopted", ["passed"]),
    ("Approved", ["passed"]),
    ("Introduced", ["introduced"]),
    ("Signed", ["signed"]),
    ("Vetoed", ["vetoed"]),
    ("veto_overridden", ["veto_overridden"]),
    ("Veto Overridden", ["veto_overridden"]),
    ("Amended", ["amended"]),
    ("", []),
    (None, []),
])
def test_doc_status_events(status, expected):
    assert doc_status_events(status) == expected
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/ingestion/test_legislative_events.py -q` → ImportError (module missing).

- [ ] **Step 3: Implement `src/ingestion/legislative_events.py`:**

```python
"""Deterministic, city-agnostic normalizer for legislative lifecycle events.

Turns the free-text `meeting_actions` fields (item_type / item_number / action)
and the doc-derived `status` fields on resolutions/legislation into a small
canonical event vocabulary. Pure keyword rules — no LLM, no city names.

Clause policy: an action string is split into clauses (';' and em/en dashes);
each clause maps to at most a couple of events. A clause matching nothing
becomes `note` context on the preceding event; if the WHOLE string maps to
nothing, parse_events returns [] and the caller records a review flag.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

EVENT_VOCABULARY = frozenset({
    "introduced", "first_reading", "referred_to_committee", "reported_out",
    "amended", "passed", "tabled", "defeated", "withdrawn",
    "signed", "vetoed", "veto_overridden",
})

_TALLY_RE = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")
_COMMITTEE_RE = re.compile(
    r"(?:referred to|moved (?:to|into))\s+(?:the\s+)?(.+?committee)", re.I)
_NUM_PREFIX_RE = re.compile(
    r"^\s*(?:bill|resolution|res\.?|ordinance|ord\.?)?\s*(?:no\.?|#)?\s*", re.I)
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|—|–)\s*")


@dataclass(frozen=True)
class LegislativeEvent:
    event: str
    committee: str = ""
    ayes: int | None = None
    nays: int | None = None
    note: str = ""


def item_kind(item_type: str | None, item_number: str | None = None) -> str | None:
    """Collapse observed item_type variants to 'resolution' | 'ordinance' | None."""
    t = (item_type or "").strip().lower()
    n = (item_number or "").strip().lower()
    if "resolution" in t:
        return "resolution"
    if "ordinance" in t or "bill" in t:
        return "ordinance"
    if n.startswith("bill"):
        return "ordinance"
    return None


def item_key(item_number: str | None) -> str:
    """Canonical identity: 'Bill 3-2026' / 'Res. No. 3-2026' / '3-2026' → '3-2026'."""
    k = _NUM_PREFIX_RE.sub("", (item_number or "").strip())
    return re.sub(r"\s+", " ", k).strip().lower()


def _clause_events(clause: str) -> list[LegislativeEvent]:
    c = clause.strip().rstrip(".")
    low = c.lower()
    if not low or low == "none":
        return []
    m = _TALLY_RE.search(c)
    ayes, nays = (int(m.group(1)), int(m.group(2))) if m else (None, None)

    # Order matters: negations and amendment votes before their substrings.
    if re.search(r"\b(did not pass|failed|defeated)\b", low):
        return [LegislativeEvent("defeated", ayes=ayes, nays=nays)]
    if re.search(r"\bamendment (?:adopted|passed|approved)\b", low):
        return [LegislativeEvent("amended", ayes=ayes, nays=nays)]
    if "withdrawn" in low:
        return [LegislativeEvent("withdrawn")]
    if "tabled" in low:
        return [LegislativeEvent("tabled")]
    if re.search(r"\b(passed|adopted|approved|final passage)\b", low):
        note = ""
        if m:
            trailing = c[m.end():].strip(" ,;—–")
            note = trailing if len(trailing) > 3 else ""
        evs = [LegislativeEvent("passed", ayes=ayes, nays=nays, note=note)]
        if "as amended" in low:
            evs.append(LegislativeEvent("amended"))
        return evs
    if "reported out" in low:
        return [LegislativeEvent("reported_out")]
    cm = _COMMITTEE_RE.search(c)
    if cm or re.search(r"\b(referred to|moved (?:to|into))\b.*\bcommittee\b", low):
        committee = cm.group(1).strip() if cm else ""
        if committee.lower() == "committee":
            committee = ""
        return [LegislativeEvent("referred_to_committee", committee=committee)]
    if "first reading" in low:
        return [LegislativeEvent("first_reading")]
    if re.search(r"\bread\b.*\brecord\b|\bintroduced\b", low):
        return [LegislativeEvent("introduced")]
    return []


def parse_events(item_type: str | None, action: str | None) -> list[LegislativeEvent]:
    events: list[LegislativeEvent] = []
    for clause in _CLAUSE_SPLIT_RE.split((action or "").strip()):
        found = _clause_events(clause)
        if found:
            events.extend(found)
        elif events and clause.strip():
            prev = events[-1]
            joined = f"{prev.note}; {clause.strip()}" if prev.note else clause.strip()
            events[-1] = replace(prev, note=joined)
    return events


def doc_status_events(status: str | None) -> list[str]:
    """Events implied by a document's own status field (legislation/resolutions)."""
    s = (status or "").strip().lower().replace("_", " ")
    if not s:
        return []
    if "veto" in s and ("overrid" in s or "override" in s):
        return ["veto_overridden"]
    if "vetoed" in s or s == "veto":
        return ["vetoed"]
    if "signed" in s or "enacted" in s:
        return ["signed"]
    if s in {"passed", "adopted", "approved"} or "final passage" in s:
        return ["passed"]
    if "amend" in s:
        return ["amended"]
    if "introduc" in s:
        return ["introduced"]
    return []
```

- [ ] **Step 4: Run tests** — `pytest tests/ingestion/test_legislative_events.py -q` → all pass. Iterate on regex details until green; do NOT weaken a test to pass it.
- [ ] **Step 5: Full non-integration suite** — `pytest -m "not integration" -q` (expect only the known pre-existing `test_prompt_warns_grants_has_no_quarter_year` failure, fixed in Task 5).
- [ ] **Step 6: Commit** — `git add src/ingestion/legislative_events.py tests/ingestion/test_legislative_events.py && git commit -m "feat(legislation): deterministic event normalizer for lifecycle tracking"`

---

### Task 2: Migration + schema DDL (tables, FK columns, views)

**Files:**
- Create: `sql/migrate_2026_07_29_legislative_status.sql`
- Modify: `sql/schema.sql` (append the same DDL before any trailing indexes; keep both in sync)

**Interfaces (produces):** tables `legislative_items`, `legislative_events`; nullable `legislative_item_id` columns on `meeting_actions`, `resolutions`, `legislation`, `votes`; views `legislative_item_status`, `legislative_item_trail`.

- [ ] **Step 1: Write the migration file** (idempotent; safe to re-run):

```sql
-- Legislative status tracking (2026-07-29). Idempotent; safe to re-run.
-- Spec: docs/superpowers/specs/2026-07-29-legislative-status-tracking-design.md

CREATE TABLE IF NOT EXISTS legislative_items (
    id             SERIAL PRIMARY KEY,
    item_kind      VARCHAR(20) NOT NULL CHECK (item_kind IN ('resolution','ordinance')),
    item_key       VARCHAR(50) NOT NULL,   -- canonical: "Bill 3-2026" -> "3-2026"
    display_number VARCHAR(50) NOT NULL,   -- first-seen raw form
    title          TEXT,                   -- longest non-empty title seen
    first_seen     DATE,                   -- earliest event date
    UNIQUE (item_kind, item_key)
);

CREATE TABLE IF NOT EXISTS legislative_events (
    id                  SERIAL PRIMARY KEY,
    legislative_item_id INTEGER NOT NULL REFERENCES legislative_items(id) ON DELETE CASCADE,
    event               VARCHAR(30) NOT NULL CHECK (event IN (
        'introduced','first_reading','referred_to_committee','reported_out',
        'amended','passed','tabled','defeated','withdrawn',
        'signed','vetoed','veto_overridden')),
    event_date          DATE,
    seq                 SMALLINT NOT NULL DEFAULT 0,
    origin              VARCHAR(10) NOT NULL CHECK (origin IN ('minutes','document')),
    meeting_action_id   INTEGER REFERENCES meeting_actions(id) ON DELETE CASCADE,
    committee           VARCHAR(120),
    ayes                SMALLINT,
    nays                SMALLINT,
    note                TEXT,
    source_chunk_id     UUID,
    source_file         VARCHAR(255),
    UNIQUE (meeting_action_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_legis_events_item ON legislative_events(legislative_item_id, event_date);

ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE resolutions     ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE legislation     ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_meeting_actions_legis_item ON meeting_actions(legislative_item_id);
CREATE INDEX IF NOT EXISTS idx_votes_legis_item ON votes(legislative_item_id);

-- Current stage per item: max event rank; deliberately date-independent so a
-- noisy OCR date can never demote a signed bill. Ranks per spec section 3.4.
CREATE OR REPLACE VIEW legislative_item_status AS
WITH ranked AS (
    SELECT ev.*, CASE ev.event
        WHEN 'introduced'            THEN 1
        WHEN 'first_reading'         THEN 1
        WHEN 'amended'               THEN 1
        WHEN 'referred_to_committee' THEN 2
        WHEN 'reported_out'          THEN 3
        WHEN 'tabled'                THEN 4
        WHEN 'withdrawn'             THEN 5
        WHEN 'defeated'              THEN 5
        WHEN 'passed'                THEN 6
        WHEN 'signed'                THEN 7
        WHEN 'vetoed'                THEN 7
        WHEN 'veto_overridden'       THEN 8
    END AS rank
    FROM legislative_events ev
), top AS (
    SELECT DISTINCT ON (legislative_item_id)
           legislative_item_id, event, event_date, rank
    FROM ranked
    ORDER BY legislative_item_id, rank DESC, event_date DESC NULLS LAST, id DESC
)
SELECT li.id AS legislative_item_id,
       li.item_kind, li.item_key, li.display_number, li.title,
       CASE top.event
           WHEN 'introduced'            THEN 'introduced'
           WHEN 'first_reading'         THEN 'introduced'
           WHEN 'amended'               THEN 'introduced'
           WHEN 'referred_to_committee' THEN 'in_committee'
           WHEN 'reported_out'          THEN 'awaiting_final_vote'
           WHEN 'tabled'                THEN 'tabled'
           WHEN 'withdrawn'             THEN 'withdrawn'
           WHEN 'defeated'              THEN 'failed'
           WHEN 'passed'                THEN 'passed'
           WHEN 'signed'                THEN 'signed'
           WHEN 'vetoed'                THEN 'vetoed'
           WHEN 'veto_overridden'       THEN 'veto_overridden'
       END AS current_stage,
       top.event_date AS stage_date,
       (CURRENT_DATE - top.event_date) AS days_in_stage,
       agg.first_event_date, agg.last_event_date, agg.meetings_touched,
       agg.latest_committee, agg.passed_ayes, agg.passed_nays
FROM legislative_items li
JOIN top ON top.legislative_item_id = li.id
LEFT JOIN LATERAL (
    SELECT MIN(e.event_date)  AS first_event_date,
           MAX(e.event_date)  AS last_event_date,
           COUNT(DISTINCT e.event_date) FILTER (WHERE e.origin = 'minutes') AS meetings_touched,
           (ARRAY_AGG(e.committee ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE COALESCE(e.committee,'') <> ''))[1] AS latest_committee,
           (ARRAY_AGG(e.ayes ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE e.event = 'passed' AND e.ayes IS NOT NULL))[1] AS passed_ayes,
           (ARRAY_AGG(e.nays ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE e.event = 'passed' AND e.nays IS NOT NULL))[1] AS passed_nays
    FROM legislative_events e
    WHERE e.legislative_item_id = li.id
) agg ON TRUE;

-- Ordered per-meeting history for one item ("what happened at each meeting").
CREATE OR REPLACE VIEW legislative_item_trail AS
SELECT li.id AS legislative_item_id,
       li.item_kind, li.item_key, li.display_number, li.title,
       ev.event, ev.event_date, ev.seq, ev.origin, ev.committee,
       ev.ayes, ev.nays, ev.note, ev.source_file
FROM legislative_items li
JOIN legislative_events ev ON ev.legislative_item_id = li.id
ORDER BY li.item_kind, li.item_key, ev.event_date NULLS LAST, ev.id;
```

- [ ] **Step 2: Append identical DDL to `sql/schema.sql`** with a `-- Legislative status tracking (see migrate_2026_07_29_legislative_status.sql)` header comment.
- [ ] **Step 3: Syntax check on scratch DB if `SCRATCH_DATABASE_URL` is set** (skip if unset — Task 6 covers it): `psql "$SCRATCH_DATABASE_URL" -f sql/schema.sql && psql "$SCRATCH_DATABASE_URL" -f sql/migrate_2026_07_29_legislative_status.sql` — both must exit 0; re-run the migration once more to prove idempotency.
- [ ] **Step 4: Commit** — `git add sql/ && git commit -m "feat(schema): legislative_items/events tables + derived status views"`

---

### Task 3: SQLStore integration (resolver + event emission)

**Files:**
- Modify: `src/storage/sql_store.py` (`__init__`, `insert_meeting_action_rows`, `insert_resolution_rows`, `insert_legislation_rows`, `insert_vote_rows`, `delete_structured_rows`; new helpers after `insert_review_flag`)
- Test: `tests/storage/test_legislative_tracking.py` (fake-cursor pattern from `tests/storage/test_transaction.py`)

**Interfaces:**
- Consumes Task 1: `item_kind`, `item_key`, `parse_events`, `doc_status_events`, `LegislativeEvent`.
- Produces: `SQLStore.resolve_legislative_item(kind: str, raw_number: str, title: str = "", event_date: str | None = None) -> int | None`; `SQLStore._legislative_tracking_enabled() -> bool`; `SQLStore._insert_legislative_events(item_id, events, *, event_date, origin, meeting_action_id, source_chunk_id, source_file) -> None`. Backfill (Task 4) reuses all three.

Key behaviors:
- `__init__` gains `self._legis_enabled: Optional[bool] = None`; the probe runs `SELECT to_regclass('legislative_items') AS t` once, caches, and returns False on any exception → **ingest works unchanged on a DB without the migration**.
- `insert_meeting_action_rows`: per row — resolve kind/item; INSERT gains `legislative_item_id` column and `RETURNING id`; parse events; insert events with `origin='minutes'`, `meeting_action_id`, `seq` = list index, `event_date` = row's meeting_date. Trackable row (kind and key both resolve) with a non-empty action but zero parsed events → `insert_review_flag(source_file, "normalize", "unmapped meeting action", f"{item_type} {item_number}: {action}")`. Note: `review_flags.stage` is VARCHAR(20) — "normalize" fits.
- `insert_resolution_rows` / `insert_legislation_rows`: resolve item (`'resolution'`/`'ordinance'`), INSERT gains `legislative_item_id`; emit `doc_status_events(status)` with `origin='document'`, `meeting_action_id=None`, `event_date` = adopted_date.
- `insert_vote_rows`: resolve only (populate `legislative_item_id`); no events.
- `delete_structured_rows`: add `"legislative_events"` to the table list **before** `meeting_actions` (document-origin events carry `source_file`; minutes-origin also cascade via `meeting_action_id`).
- All new writes are no-ops when `_legislative_tracking_enabled()` is False — the INSERT column lists must then stay exactly as today (two SQL variants per method).

- [ ] **Step 1: Write failing tests** — fake cursor that records `(sql, params)` and returns canned rows:

```python
"""Unit tests for legislative-tracking hooks in SQLStore (no live DB).

A fake cursor records executed SQL and serves canned fetch results so we can
assert the resolver/event wiring without PostgreSQL. Integration coverage is
in tests/storage/test_legislative_status_integration.py.
"""
from src.storage.sql_store import SQLStore


class FakeCursor:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=None):
        self.store.executed.append((" ".join(sql.split()), params))
        s = sql.lower()
        if "to_regclass" in s:
            self._next = {"t": "legislative_items" if self.store.migrated else None}
        elif "insert into legislative_items" in s:
            self._next = {"id": 42}
        elif "returning id" in s:
            self._next = {"id": 7}
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def close(self):
        pass


class FakeConn:
    closed = False
    def __init__(self, store): self.store = store
    def cursor(self): return FakeCursor(self.store)
    def commit(self): pass
    def rollback(self): pass


def make_store(migrated=True):
    s = SQLStore.__new__(SQLStore)
    s.executed = []
    s.migrated = migrated
    s._conn = FakeConn(s)
    s._in_txn = False
    s._legis_enabled = None
    return s


def _sqls(store):
    return [sql for sql, _ in store.executed]


def test_meeting_action_insert_emits_events():
    s = make_store()
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution",
          "item_number": "20-2026", "title": "T", "action": "Passed 7-0", "committee": ""}],
        "chunk-1", "minutes.pdf")
    joined = "\n".join(_sqls(s))
    assert "INSERT INTO legislative_items" in joined
    assert "INSERT INTO legislative_events" in joined
    ev_params = [p for sql, p in s.executed if "INSERT INTO legislative_events" in sql][0]
    assert "passed" in ev_params
    assert 7 in ev_params and 0 in ev_params           # tally captured


def test_meeting_action_without_migration_is_legacy_shape():
    s = make_store(migrated=False)
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution",
          "item_number": "20-2026", "title": "T", "action": "Passed 7-0", "committee": ""}],
        "chunk-1", "minutes.pdf")
    joined = "\n".join(_sqls(s))
    assert "legislative_items" not in joined.replace("to_regclass('legislative_items')", "")
    assert "INSERT INTO legislative_events" not in joined
    assert "legislative_item_id" not in joined          # legacy column list untouched


def test_untrackable_item_type_inserts_without_events():
    s = make_store()
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Minutes Approval",
          "item_number": "", "title": "", "action": "Approved", "committee": ""}],
        "chunk-1", "minutes.pdf")
    assert not any("legislative_events" in q for q in _sqls(s))
    assert not any("review_flags" in q for q in _sqls(s))   # untrackable != unmapped


def test_unmapped_action_flags_for_review():
    s = make_store()
    s.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution",
          "item_number": "9-2026", "title": "T",
          "action": "Announced for March 31, 2026 at 5:30 PM", "committee": ""}],
        "chunk-1", "minutes.pdf")
    flags = [p for sql, p in s.executed if "review_flags" in sql]
    assert flags and flags[0][1] == "normalize"


def test_resolution_insert_emits_document_event():
    s = make_store()
    s.insert_resolution_rows(
        [{"resolution_number": "20-2026", "title": "T", "amount": None, "vendor": "",
          "department": "", "adopted_date": "2026-03-03", "status": "Adopted"}],
        "chunk-2", "res20.pdf")
    ev = [p for sql, p in s.executed if "INSERT INTO legislative_events" in sql]
    assert ev and "document" in ev[0]
    assert "passed" in ev[0]


def test_legislation_signed_status_emits_signed_event():
    s = make_store()
    s.insert_legislation_rows(
        [{"bill_number": "3-2026", "title": "T", "sponsor": "", "amount": None,
          "adopted_date": "2026-04-01", "status": "Signed"}],
        "chunk-3", "bill3.pdf")
    ev = [p for sql, p in s.executed if "INSERT INTO legislative_events" in sql]
    assert ev and "signed" in ev[0]


def test_vote_rows_link_item_without_events():
    s = make_store()
    s.insert_vote_rows(
        [{"resolution_number": "20-2026", "council_member": "A. Member", "vote": "yes"}],
        "chunk-4", "res20.pdf")
    joined = "\n".join(_sqls(s))
    assert "INSERT INTO legislative_items" in joined
    assert "INSERT INTO legislative_events" not in joined


def test_delete_structured_rows_covers_legislative_events():
    s = make_store()
    s.delete_structured_rows("minutes.pdf")
    assert any("legislative_events" in q for q in _sqls(s))
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/storage/test_legislative_tracking.py -q` → assertions fail (no legislative SQL emitted yet).
- [ ] **Step 3: Implement.** Add imports (`from src.ingestion.legislative_events import doc_status_events, item_key, item_kind, parse_events`), the probe + resolver + `_insert_legislative_events` helpers, and rework the four insert methods with the two-variant SQL (legacy vs tracking-enabled). Resolver SQL:

```sql
INSERT INTO legislative_items (item_kind, item_key, display_number, title, first_seen)
VALUES (%s, %s, %s, NULLIF(%s, ''), %s)
ON CONFLICT (item_kind, item_key) DO UPDATE SET
    title = CASE WHEN length(coalesce(EXCLUDED.title, '')) > length(coalesce(legislative_items.title, ''))
                 THEN EXCLUDED.title ELSE legislative_items.title END,
    first_seen = LEAST(coalesce(legislative_items.first_seen, EXCLUDED.first_seen),
                       coalesce(EXCLUDED.first_seen, legislative_items.first_seen))
RETURNING id
```

Event insert SQL (one execute per event, `ON CONFLICT (meeting_action_id, seq) DO NOTHING`):

```sql
INSERT INTO legislative_events
  (legislative_item_id, event, event_date, seq, origin, meeting_action_id,
   committee, ayes, nays, note, source_chunk_id, source_file)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (meeting_action_id, seq) DO NOTHING
```

- [ ] **Step 4: Run tests** — `pytest tests/storage/test_legislative_tracking.py tests/storage/test_transaction.py -q` → pass (existing transaction tests must stay green).
- [ ] **Step 5: Full non-integration suite** — `pytest -m "not integration" -q`.
- [ ] **Step 6: Commit** — `git commit -m "feat(storage): resolve legislative items and emit lifecycle events at ingest"`

---

### Task 4: Backfill script

**Files:**
- Create: `scripts/backfill_legislative_events.py`
- Test: covered by Task 6 integration test (`from scripts.backfill_legislative_events import rebuild`); plus a pure-function unit test file is NOT needed — `derive()` is exercised through Task 1's normalizer tests and Task 6.

**Interfaces:**
- Consumes: Task 1 functions; Task 3 `resolve_legislative_item` / `_insert_legislative_events`.
- Produces: `rebuild(store: SQLStore, dry_run: bool = False) -> dict` returning `{"items": int, "events": int, "flagged": int, "skipped_no_number": int}`; CLI `python scripts/backfill_legislative_events.py [--dry-run]`.

Full-rebuild semantics (idempotent by construction): inside one `store.transaction()` — `DELETE FROM legislative_events`, re-derive everything from `meeting_actions` + `resolutions` + `legislation` + `votes`, update each raw row's `legislative_item_id`. Dry-run computes and prints the same summary from a pure pass over fetched rows, writes nothing (no review flags either).

- [ ] **Step 1: Write the script:**

```python
"""Rebuild legislative_items / legislative_events from raw rows.

Fully derived data: safe to re-run any time (full rebuild inside one
transaction). Never call with production DATABASE_URL until the migration has
been applied by a human (repo rule). No LLM calls.

Usage:
    python scripts/backfill_legislative_events.py [--dry-run]
"""
import argparse
import sys
from collections import Counter

sys.path.insert(0, ".")

from src.ingestion.legislative_events import doc_status_events, item_key, item_kind, parse_events
from src.storage.sql_store import SQLStore


def _fetch(store, sql):
    with store.cursor() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def rebuild(store: SQLStore, dry_run: bool = False) -> dict:
    actions = _fetch(store, "SELECT * FROM meeting_actions ORDER BY meeting_date, id")
    resolutions = _fetch(store, "SELECT * FROM resolutions ORDER BY id")
    legislation = _fetch(store, "SELECT * FROM legislation ORDER BY id")
    votes = _fetch(store, "SELECT * FROM votes ORDER BY id")

    stats = Counter()
    if dry_run:
        for a in actions:
            kind = item_kind(a.get("item_type"), a.get("item_number"))
            if not kind:
                continue
            if not item_key(a.get("item_number")):
                stats["skipped_no_number"] += 1
                continue
            evs = parse_events(a.get("item_type"), a.get("action"))
            if evs:
                stats["events"] += len(evs)
            elif (a.get("action") or "").strip():
                stats["flagged"] += 1
            stats["items_seen"] += 1
        return dict(stats)

    with store.transaction():
        with store.cursor() as cur:
            cur.execute("DELETE FROM legislative_events")

        for a in actions:
            kind = item_kind(a.get("item_type"), a.get("item_number"))
            if not kind:
                continue
            if not item_key(a.get("item_number")):
                stats["skipped_no_number"] += 1
                continue
            item_id = store.resolve_legislative_item(
                kind, a["item_number"], a.get("title") or "", a.get("meeting_date"))
            with store.cursor() as cur:
                cur.execute("UPDATE meeting_actions SET legislative_item_id=%s WHERE id=%s",
                            (item_id, a["id"]))
            evs = parse_events(a.get("item_type"), a.get("action"))
            if evs:
                store._insert_legislative_events(
                    item_id, evs, event_date=a.get("meeting_date"), origin="minutes",
                    meeting_action_id=a["id"], source_chunk_id=a.get("source_chunk_id"),
                    source_file=a.get("source_file"))
                stats["events"] += len(evs)
            elif (a.get("action") or "").strip():
                store.insert_review_flag(
                    a.get("source_file") or "", "normalize", "unmapped meeting action",
                    f"{a.get('item_type')} {a.get('item_number')}: {a.get('action')}")
                stats["flagged"] += 1

        for kind, rows, number_col in (("resolution", resolutions, "resolution_number"),
                                       ("ordinance", legislation, "bill_number")):
            for r in rows:
                if not item_key(r.get(number_col)):
                    stats["skipped_no_number"] += 1
                    continue
                item_id = store.resolve_legislative_item(
                    kind, r[number_col], r.get("title") or "", r.get("adopted_date"))
                table = "resolutions" if kind == "resolution" else "legislation"
                with store.cursor() as cur:
                    cur.execute(f"UPDATE {table} SET legislative_item_id=%s WHERE id=%s",
                                (item_id, r["id"]))
                from src.ingestion.legislative_events import LegislativeEvent
                evs = [LegislativeEvent(e) for e in doc_status_events(r.get("status"))]
                if evs:
                    store._insert_legislative_events(
                        item_id, evs, event_date=r.get("adopted_date"), origin="document",
                        meeting_action_id=None, source_chunk_id=r.get("source_chunk_id"),
                        source_file=r.get("source_file"))
                    stats["events"] += len(evs)

        for v in votes:
            if not item_key(v.get("resolution_number")):
                continue
            item_id = store.resolve_legislative_item("resolution", v["resolution_number"])
            with store.cursor() as cur:
                cur.execute("UPDATE votes SET legislative_item_id=%s WHERE id=%s",
                            (item_id, v["id"]))

        with store.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM legislative_items")
            stats["items"] = cur.fetchone()["n"]
    return dict(stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report counts; write nothing")
    args = ap.parse_args()
    store = SQLStore()
    store.connect()
    stats = rebuild(store, dry_run=args.dry_run)
    mode = "DRY RUN — nothing written" if args.dry_run else "rebuilt"
    print(f"Legislative status backfill ({mode}):")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    store.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Sanity check** — `python -c "import scripts.backfill_legislative_events"` (imports resolve; no DB touched at import time). Move the inner `LegislativeEvent` import to the top of the file while implementing (shown inline above only to keep the diff hunk local — top-level import is the correct final form).
- [ ] **Step 3: Commit** — `git commit -m "feat(scripts): full-rebuild backfill for legislative status events"`

---

### Task 5: Classifier schema reference + stale prompt test fix

**Files:**
- Modify: `src/query/classifier.py` (schema reference block, ~lines 47-58)
- Modify: `tests/query/test_classifier_prompt.py` (fix pre-existing `test_prompt_warns_grants_has_no_quarter_year` failure — its expected literal drifted from the prompt; update the assertion to the current wording, and add view assertions)

- [ ] **Step 1: Add failing assertions** to `tests/query/test_classifier_prompt.py`:

```python
def test_prompt_documents_legislative_status_views():
    assert "legislative_item_status" in _CLASSIFY_PROMPT
    assert "legislative_item_trail" in _CLASSIFY_PROMPT
    assert "current_stage" in _CLASSIFY_PROMPT
    assert "in_committee" in _CLASSIFY_PROMPT
```

Also read the failing grants test and repair its expected literal against the actual prompt text (`grep -n "NO \`quarter\`" src/query/classifier.py`), keeping the test's intent: the prompt must still warn that grants has no quarter/year.

- [ ] **Step 2: Run to verify** — `pytest tests/query/test_classifier_prompt.py -q` → new test fails, grants test now passes.
- [ ] **Step 3: Extend the schema reference in `classifier.py`** — after the `meeting_actions` line add:

```
- legislative_item_status(item_kind, item_key, display_number, title, current_stage, stage_date, days_in_stage, first_event_date, last_event_date, meetings_touched, latest_committee, passed_ayes, passed_nays) — VIEW: current lifecycle stage per resolution/ordinance. current_stage one of: 'introduced','in_committee','awaiting_final_vote','tabled','withdrawn','failed','passed','signed','vetoed','veto_overridden'. item_key has no 'Bill'/'Res.' prefix (e.g. '3-2026').
- legislative_item_trail(item_kind, item_key, display_number, title, event, event_date, origin, committee, ayes, nays, note, source_file) — VIEW: ordered event history for an item across meetings.
```

And extend the routing hint: `For "where is Bill X" / "status of Resolution Y" → legislative_item_status; for "what happened to it at each meeting" → legislative_item_trail (filter item_key ILIKE without prefixes). For "what's stuck in committee" → legislative_item_status WHERE current_stage='in_committee'.` Also add both view names to the no-`quarter`/`year` warning list.

- [ ] **Step 4: Run** — `pytest tests/query/ -q` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "feat(query): expose legislative status views to the SQL router"`

---

### Task 6: Scratch-DB integration test (end-to-end)

**Files:**
- Create: `tests/storage/test_legislative_status_integration.py`

**Interfaces (consumes):** Task 2 DDL, Task 3 store methods, Task 4 `rebuild`.

Gated on `SCRATCH_DATABASE_URL` (never the live `DATABASE_URL` — backfill deletes derived rows). Marked `integration`.

- [ ] **Step 1: Write the test:**

```python
"""End-to-end legislative status tracking against a scratch database.

Requires SCRATCH_DATABASE_URL (a throwaway DB). Applies schema + migration,
seeds observed-shape raw rows, runs the backfill, and asserts stages, the
multi-meeting trail, review flags, and idempotency. Never touches DATABASE_URL.
"""
import os
import subprocess

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("SCRATCH_DATABASE_URL"),
                       reason="SCRATCH_DATABASE_URL not set"),
]

from scripts.backfill_legislative_events import rebuild
from src.storage.sql_store import SQLStore


@pytest.fixture()
def store():
    url = os.environ["SCRATCH_DATABASE_URL"]
    subprocess.run(["psql", url, "-q", "-f", "sql/schema.sql"], check=True)
    subprocess.run(["psql", url, "-q", "-f",
                    "sql/migrate_2026_07_29_legislative_status.sql"], check=True)
    s = SQLStore.__new__(SQLStore)
    from config import get_settings
    s.settings = get_settings()
    s.settings = type(s.settings)(**{**s.settings.model_dump(), "database_url": url})
    s._conn = None
    s._in_txn = False
    s._legis_enabled = None
    s.connect()
    with s.cursor() as cur:
        for t in ("legislative_events", "votes", "meeting_actions", "resolutions",
                  "legislation", "review_flags", "legislative_items"):
            cur.execute(f"DELETE FROM {t}")
    yield s
    s.close()


def _seed(store):
    # Bill 1-2026: multi-meeting ordinance trail (observed shape)
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-02-10", "item_type": "ordinance", "item_number": "1-2026",
          "title": "Budget amendment", "action": "first reading; referred to committee",
          "committee": "Administration Committee"}],
        "c1", "minutes_feb.pdf")
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Ordinance for Final Passage",
          "item_number": "Bill 1-2026", "title": "Budget amendment",
          "action": "Passed 7-0 as amended", "committee": ""}],
        "c2", "minutes_mar.pdf")
    # Resolution 20-2026: referred, never passed
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution", "item_number": "20-2026",
          "title": "Grant application", "action": "Referred to Public Safety Committee",
          "committee": ""}],
        "c3", "minutes_mar.pdf")
    # Resolution 5-2026: tabled
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "resolution", "item_number": "5-2026",
          "title": "T", "action": "tabled", "committee": ""}],
        "c4", "minutes_mar.pdf")
    # Unmapped action on a trackable item -> review flag
    store.insert_meeting_action_rows(
        [{"meeting_date": "2026-03-03", "item_type": "Resolution", "item_number": "9-2026",
          "title": "T", "action": "Announced for March 31, 2026 at 5:30 PM",
          "committee": ""}],
        "c5", "minutes_mar.pdf")
    # Signed bill via its own document
    store.insert_legislation_rows(
        [{"bill_number": "1-2026", "title": "Budget amendment", "sponsor": "",
          "amount": None, "adopted_date": "2026-03-20", "status": "Signed"}],
        "c6", "bill1.pdf")


def _stage(store, kind, key):
    with store.cursor() as cur:
        cur.execute("SELECT current_stage FROM legislative_item_status "
                    "WHERE item_kind=%s AND item_key=%s", (kind, key))
        row = cur.fetchone()
        return row["current_stage"] if row else None


def test_end_to_end_stages_and_trail(store):
    _seed(store)
    assert _stage(store, "ordinance", "1-2026") == "signed"       # doc beats minutes
    assert _stage(store, "resolution", "20-2026") == "in_committee"
    assert _stage(store, "resolution", "5-2026") == "tabled"

    with store.cursor() as cur:
        cur.execute("SELECT event, origin FROM legislative_item_trail "
                    "WHERE item_kind='ordinance' AND item_key='1-2026' ORDER BY event_date, seq")
        trail = [(r["event"], r["origin"]) for r in cur.fetchall()]
    assert ("first_reading", "minutes") in trail
    assert ("referred_to_committee", "minutes") in trail
    assert ("passed", "minutes") in trail
    assert ("amended", "minutes") in trail
    assert ("signed", "document") in trail

    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM review_flags "
                    "WHERE stage='normalize' AND resolved=FALSE")
        assert cur.fetchone()["n"] == 1

    # "Bill 1-2026" and "1-2026" resolved to ONE item
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_items "
                    "WHERE item_kind='ordinance' AND item_key='1-2026'")
        assert cur.fetchone()["n"] == 1


def test_backfill_rebuild_is_idempotent(store):
    _seed(store)
    first = rebuild(store)
    second = rebuild(store)
    assert first["events"] == second["events"] > 0
    assert _stage(store, "ordinance", "1-2026") == "signed"
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_events")
        n = cur.fetchone()["n"]
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_items")
        items = cur.fetchone()["n"]
    assert n == second["events"]
    assert items == second["items"]


def test_dry_run_writes_nothing(store):
    _seed(store)
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_events")
        before = cur.fetchone()["n"]
    stats = rebuild(store, dry_run=True)
    assert stats.get("events", 0) > 0
    with store.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM legislative_events")
        assert cur.fetchone()["n"] == before
```

- [ ] **Step 2: Provision scratch DB and run** — `createdb ckb_scratch 2>/dev/null; SCRATCH_DATABASE_URL=postgresql://localhost:5432/ckb_scratch pytest tests/storage/test_legislative_status_integration.py -q` (local Postgres 16 is available at `/opt/homebrew/var/postgresql@16`). All pass. If Settings has no easy override, adjust the fixture to build `SQLStore` with a Settings copy — keep production `DATABASE_URL` untouched.
- [ ] **Step 3: Full suite** — `pytest -m "not integration" -q` green; `SCRATCH_DATABASE_URL=... pytest tests/storage/test_legislative_status_integration.py -q` green.
- [ ] **Step 4: Commit** — `git commit -m "test(storage): scratch-DB end-to-end coverage for legislative status"`

---

## Out of scope (deliberate)

- Applying the migration/backfill to the production DB — human-run, per repo rule.
- Dashboard legislation-tracker UI (follow-on).
- Re-ingesting Resolution 20's votes (owner-gated, LLM cost).
- Tally-vs-votes reconciliation validator.

## Self-review notes

- Spec coverage: §3.1→Task 2+3 (resolver), §3.2→Task 1, §3.3→Tasks 2/3/4, §3.4→Task 2 views + Task 6 asserts, §3.5→Tasks 4/5, §5→Tasks 1/6. Anomaly review-flag on minutes-events-after-terminal (§3.4) is deferred to the backfill's `flagged` counter — acceptable v1 (documented deviation).
- Clause policy deviation from spec §3.2 (unmatched clause → note, whole-string-only flags) is stated in Task 1 and matches the spec's own `note` column intent.
