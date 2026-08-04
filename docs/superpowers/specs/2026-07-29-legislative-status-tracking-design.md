# Legislative Status Tracking — Design

**Date:** 2026-07-29
**Status:** Draft — pending owner review
**Goal:** Let the clerk follow each resolution/ordinance through its lifecycle
(introduced → in committee → passed → signed/vetoed) across meetings, as a
SQL-queryable status model built from data we already ingest.

## 1. Problem

The clerk's #1 legislative ask (2026-07-29 meeting): "where is Bill X right
now, and what happened to it at each meeting?" Today that answer is scattered:

- `meeting_actions` already records the trail (e.g. Bill 1-2026: first reading
  → referred to committee → six amendments → final passage, across 2 meetings)
  but as **free text**: `item_type` has 9 case/phrasing variants ("resolution",
  "Ordinance for Final Passage", "Committee Report"…), `item_number` is
  inconsistent ("1-2026" vs "Bill 3-2026"), and `action` mixes several events
  plus vote tallies in one string ("first reading; referred to committee",
  "Passed 6-1, with Councilwoman Davis voting in the negative").
- `resolutions.status` is near-useless for lifecycle ("Passed"/"Adopted"/"").
  `legislation.status` carries the doc-derived end state (signed/vetoed) that
  minutes never show.
- **Coverage gap:** 79 distinct resolution numbers appear in `meeting_actions`
  but only 21 have a `resolutions` row (2 for `legislation`). Most items exist
  *only* as minutes actions — the status model cannot hang off the
  resolutions/legislation tables.

## 2. Approaches considered

**A. Canonical event log + derived-status view (recommended).** Normalize each
`meeting_actions` row into zero-or-more canonical events against a
`legislative_items` dimension discovered from data; current stage is a SQL view
over events. Deterministic Python rules, no LLM spend. Keeps the full trail
(the clerk's actual ask), survives re-ingest, follows the entity-dimension
pattern already approved in the 2026-07-28 schema-relationships spec.

**B. Materialized `current_status` column maintained at ingest.** Simplest,
but loses the per-meeting trail, drifts when documents re-ingest out of order,
and inherits the duplicate-insert problem. Answers "what stage" but not "what
happened" — rejected.

**C. LLM normalization of action strings.** Highest fidelity on weird strings,
but costs money (owner's funds rule) and is unnecessary: the observed
vocabulary is small and keyword-separable. Unknown strings go to human review
instead — rejected.

## 3. Design (Approach A)

Four additive pieces, same philosophy as the 2026-07-28 spec: raw text kept as
audit trail, canonical layer derived and recomputable, ambiguity → review
flags, nothing guessed, city-agnostic.

### 3.1 `legislative_items` dimension — the spine

One row per real-world piece of legislation, keyed on kind + canonical number.
Auto-created on first sight (resolver pattern shared with
departments/council_members/vendors); ships empty, populates from data.

```sql
CREATE TABLE IF NOT EXISTS legislative_items (
    id             SERIAL PRIMARY KEY,
    item_kind      VARCHAR(20) NOT NULL CHECK (item_kind IN ('resolution','ordinance')),
    item_key       VARCHAR(50) NOT NULL,   -- canonical: "Bill 3-2026" → "3-2026"
    display_number VARCHAR(50) NOT NULL,   -- first-seen raw form
    title          TEXT,                   -- longest non-empty title seen
    first_seen     DATE,                   -- earliest event date
    UNIQUE (item_kind, item_key)
);
```

New nullable FK columns: `meeting_actions.legislative_item_id`,
`resolutions.legislative_item_id`, `legislation.legislative_item_id`,
`votes.legislative_item_id` — so the whole loop (minutes → item → full record
→ votes) traverses through one spine even when the resolutions/legislation row
doesn't exist. Composes with, but does not require, the 2026-07-28 plan's
direct cross-link FKs.

### 3.2 Deterministic normalizer — `src/ingestion/legislative_events.py`

Pure functions, sibling of `entity_keys.py`, city-agnostic keyword rules:

- `item_kind(item_type, item_number) -> 'resolution' | 'ordinance' | None` —
  collapses the 9 observed `item_type` variants; "Ordinance for Final
  Passage"-style types also contribute an event hint. `None` (minutes
  approval, new business, committee reports) → no lifecycle tracking in v1.
- `item_key(item_number) -> str` — strips "Bill "/"Res. " prefixes,
  whitespace, case → "3-2026".
- `parse_events(item_type, action) -> list[Event]` — splits multi-event
  strings on `;`/`—` clauses and maps each clause by keyword. An
  `Event` is `(event, committee, ayes, nays, note)`; tallies parsed from
  "Passed 6-1"-style text.

Canonical event vocabulary (grounded in every observed string):

| Event | Matches (case-insensitive) |
| --- | --- |
| `introduced` | "read into record", "introduced" |
| `first_reading` | "first reading" |
| `referred_to_committee` | "referred to …", "moved to/into … committee" |
| `reported_out` | "reported out" |
| `amended` | "amendment adopted/passed …", "read … for amendment", "as amended" (alongside `passed`) |
| `passed` | "passed N-N", "final passage", "adopted", "approved" |
| `tabled` | "tabled" |
| `defeated` | "failed", "defeated", "motion … did not pass" |
| `withdrawn` | "withdrawn" |
| `signed` / `vetoed` / `veto_overridden` | doc-derived only (§3.3) |

Order matters: "Passed 6-1, with X voting in the negative" is `passed` (the
dissent clause never matches `defeated` — tally check `ayes > nays` guards
this). A clause matching nothing yields no event and a `review_flags` row
(`stage='normalize'`) with the raw string; the item simply stays at its prior
stage. Never guessed.

### 3.3 `legislative_events` table — the canonical trail

```sql
CREATE TABLE IF NOT EXISTS legislative_events (
    id                  SERIAL PRIMARY KEY,
    legislative_item_id INTEGER NOT NULL REFERENCES legislative_items(id) ON DELETE CASCADE,
    event               VARCHAR(30) NOT NULL,   -- vocabulary above, CHECK-constrained
    event_date          DATE,                   -- meeting_date or adopted_date
    seq                 SMALLINT NOT NULL DEFAULT 0,  -- clause order within one action
    origin              VARCHAR(10) NOT NULL CHECK (origin IN ('minutes','document')),
    meeting_action_id   INTEGER REFERENCES meeting_actions(id) ON DELETE CASCADE,
    committee           VARCHAR(120),
    ayes                SMALLINT,
    nays                SMALLINT,
    note                TEXT,                   -- residual clause text (recusals, amendment substance)
    source_chunk_id     UUID,
    source_file         VARCHAR(255),
    UNIQUE (meeting_action_id, seq)
);
```

Two origins:

- **`minutes`** — from `meeting_actions` via the normalizer (provenance:
  `meeting_action_id` + passthrough chunk/file). Deleting or re-ingesting an
  action cascades its events away — events are always recomputable from raw.
- **`document`** — the item's own record: `legislation.status`
  signed/vetoed/veto_overridden and `resolutions`/`legislation`
  "passed/adopted" + `adopted_date` become events too, since minutes never
  show the mayor's signature. Dated by `adopted_date` (nullable).

Duplicate `meeting_actions` rows (known: no de-dup at insert) produce
duplicate events; `UNIQUE (meeting_action_id, seq)` makes recomputation
idempotent, and stage derivation (max-rank, below) is unaffected by
duplicates. Repeated `amended` rows are often genuinely distinct amendments,
so no cross-action de-dup in v1.

### 3.4 `legislative_item_status` view — current stage, derived

No stored status to drift. Stage is `max(rank)` over an item's events —
deliberately date-independent, because OCR dates are noisy and terminal
events must win even if dated wrong:

| Rank | Event(s) | Stage |
| --- | --- | --- |
| 1 | introduced, first_reading | `introduced` |
| 2 | referred_to_committee | `in_committee` |
| 3 | reported_out | `awaiting_final_vote` |
| 4 | tabled | `tabled` |
| 5 | withdrawn / defeated | `withdrawn` / `failed` |
| 6 | passed | `passed` |
| 7 | signed / vetoed | `signed` / `vetoed` |
| 8 | veto_overridden | `veto_overridden` |

The view exposes: item identity, `current_stage`, `stage_date`, first/last
event dates, meetings touched, committee (latest referral), tallies of the
passage vote, and `days_in_stage`. A companion `legislative_item_trail` view
returns the ordered event list per item — the clerk's "what happened at each
meeting". At this scale (hundreds of rows) plain views suffice; no
materialization.

Anomaly rule: an event ranked ≥6 arriving while `tabled` is fine (items come
off the table); a `minutes`-origin event arriving after `signed`/`vetoed` →
`review_flags` (`stage='status'`), event kept.

**Resolutions vs ordinances:** same machinery, different typical paths.
Resolutions usually terminate at `passed` (no mayoral signature); ordinances
continue to `signed`/`vetoed`. The view doesn't force either shape — a
resolution that never leaves committee shows `in_committee`, which is
precisely what the clerk wants to see.

### 3.5 Population & integration

- **Backfill:** `scripts/backfill_legislative_events.py` — resolves items,
  emits events from all three source tables, reports unmapped-string and
  empty-`item_number` counts. Runs against `SCRATCH_DATABASE_URL` first;
  production apply is a separate human-run step (repo constraint). No LLM
  calls anywhere.
- **Ingest, go-forward:** `insert_meeting_action_rows`,
  `insert_resolution_rows`, `insert_legislation_rows` resolve the item FK and
  emit events inline (same transaction, same pattern as the planned dimension
  resolvers).
- **Query pipeline:** the two views are added to the classifier's SQL schema
  description so `ask()` can answer "status of Bill 1-2026" / "what's stuck in
  committee" — no pipeline code change. Dashboard legislation tracker UI is a
  **follow-on, out of scope here**.

## 4. Known limits / open items

- **Res 20 votes:** missing pending an approved re-ingest (owner-gated, LLM
  cost). Does not affect this model — member votes are a cross-check, not a
  status input; Res 20's trail will simply lack vote detail.
- Items with empty `item_number` can't be tracked; counted and reported by the
  backfill, rows left untouched.
- "Committee Report / Reported out" rows (9 observed) don't name the item in
  `item_number`; v1 skips them (flagged), so `awaiting_final_vote` will be
  under-populated until extraction improves.
- Tally-vs-`votes`-table reconciliation is a natural later validator; out of
  scope.
- Sequencing vs the 2026-07-28 schema-relationships plan: independent — no
  shared tables or columns; whichever lands second rebases trivially.

## 5. Testing

- Unit (no DB): normalizer table-driven tests over **every distinct observed
  `(item_type, action)` string** (45 as of today) plus adversarial cases
  ("Passed 6-1 … voting in the negative" ≠ defeated; multi-clause splits;
  "Bill 3-2026" vs "3-2026" identity).
- Integration (`@pytest.mark.integration`): backfill on a seeded scratch
  schema → assert item count, trail for a multi-meeting ordinance, stage for
  tabled/passed/in-committee fixtures, review flags for an unmapped string,
  idempotency (run twice → identical).
