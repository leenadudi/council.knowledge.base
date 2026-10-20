# "Questions for Next Quarter" — Design Spec

**Date:** 2026-07-09
**Status:** Approved (design), pending implementation plan
**Feature owner:** Council KB / ClerkFlow

## Problem

City Clerk Truesdale's most-repeated ask is a tool that helps him *generate sharper
questions to put into the next quarterly report* — a way to track departmental
follow-through and prompt council members with specific, data-grounded questions.
The app today can *answer* questions (Ask) but cannot *propose* them. The only
"suggestions" are a hardcoded static list.

This feature closes that loop: for each department, surface the gaps worth asking
about next quarter, phrased as clerk-ready questions.

## Scope (v1)

Per-department "prep sheet" of generated questions, driven by three deterministic
gap signals over data already in the database, with an on-demand Haiku phrasing pass.

**In scope**
- Deterministic gap detection (free, always available).
- Three signals: goals with no reported progress; stalled/repeated goals; budget pace anomaly.
- Per-department presentation with copy-to-clipboard / copy-all.
- On-demand, cached LLM phrasing (Haiku) — polished wording is the default view.

**Out of scope (v1)**
- "Department went quiet" signal (considered, deferred).
- Metrics-based signals (metrics are over-extracted/noisy today — see strategic memory).
- Grant-drawdown signals (per-grant spend is not linked in the schema).
- Persistent cross-restart cache table (in-memory cache only for v1).
- Editing/saving questions back into the app; export beyond clipboard.

## Cost constraint (hard requirement)

Per the standing "ask before LLM spend" rule, the Haiku phrasing pass **must never
run inside the always-on dashboard build** (`DashboardAggregator.build()` / the
`/dashboard/data` route, which refreshes on a 90s cache). It runs only on demand,
per department, when a department's sheet is opened, and caches the result keyed to
the underlying findings so identical data never re-triggers a call. Detection is
100% deterministic SQL and carries no cost.

## Architecture

Approach A (split): free deterministic detector + on-demand cached phraser.

### 1. Detection module — `src/dashboard/review_questions.py`

A `ReviewQuestions` builder mirroring `DashboardAggregator` conventions:
- constructed with the same `sql_store`; reuses `DashboardAggregator._dept_key` /
  `_dept_key_base` / `_dept_display` for canonical department grouping.
- `build()` returns `{ "period": {year, quarter}, "departments": [ {department, findings:[...]} ] }`
  keyed by canonical department, sorted by department name.

Each **finding** is a dict:
```
{
  "signal": "goal_no_progress" | "goal_stalled" | "budget_pace",
  "department": "<display name>",
  "question": "<templated question string>",   # free fallback wording
  "evidence": { ... signal-specific raw fields ... }  # for citation + phrasing input
}
```

#### Signal definitions

- **`goal_no_progress`** — goals where `status` IS NULL or `TRIM(status) = ''`, and a
  non-empty `target` exists, for the latest reporting period.
  Template: *"[Dept]'s goal '[goal_title]' (target: [target]) shows no reported
  progress as of [Qn YYYY]. What's the current status?"*
  Evidence: `{goal_id, goal_title, target, year, quarter}`.

- **`goal_stalled`** — the same normalized `goal_title` (lowercased, whitespace-collapsed)
  appears for a department across ≥2 distinct periods with no status change (status
  null/empty in the latest, or identical across periods).
  Template: *"'[goal_title]' has carried across [N] quarters ([earliest]→[latest])
  with no update — what's blocking completion?"*
  Evidence: `{goal_title, periods:[...], count}`.

- **`budget_pace`** — per department (latest period, excluding Munis `%total%` rows,
  canonical-keyed), compute `pace = ytd_expended / revised_budget` and compare to
  expected pace for the quarter: Q1≈0.25, Q2≈0.50, Q3≈0.75, Q4≈1.00. Flag when
  `pace > 1.5 × expected` (ahead/overspending) or `pace < 0.5 × expected` (behind),
  requiring `revised_budget > 0`.
  Template (ahead): *"[Dept] is at [X]% of its revised budget by [Qn] (≈[Y]% expected) —
  what's driving the elevated spend?"*
  Template (behind): *"[Dept] has spent only [X]% of its revised budget by [Qn]
  (≈[Y]% expected) — why is spending behind pace?"*
  Evidence: `{department, revised_budget, ytd_expended, pace, expected, direction}`.

`build()` is wired into `DashboardAggregator.build()` as a new panel
`"review_questions"` via the existing `_safe()` wrapper — deterministic, no LLM.

### 2. Phrasing endpoint — `/questions/<dept>`

Flask route that:
1. Runs `ReviewQuestions.build()` (deterministic SQL, cheap) and selects the target
   department's findings. Always a fresh detection pass — no dependency on the
   dashboard cache — so the questions reflect current data.
2. Computes a stable hash of that department's findings list.
3. On cache miss, issues **one** Haiku call: system prompt instructs it to rewrite
   the templated questions into natural, specific, clerk-ready questions **without
   inventing facts** (only rephrase; keep every number/target/name from the
   templated input). Returns a JSON list aligned 1:1 with the input findings.
4. Caches `{dept_hash → polished_questions}` in-memory (module-level dict).
5. Returns `{ "department": ..., "questions": [ {question, signal, evidence}... ],
   "polished": true|false }`.

Cache key = hash of findings → identical data is a guaranteed cache hit (no spend).
If the Haiku call fails, the endpoint returns the templated wording with
`"polished": false` (graceful degradation, still useful, still free).

### 3. Frontend — "Next quarter" view

- New nav item under **Explore**: "Next quarter".
- `renderQuestions()` reads `D.review_questions`.
- Department picker (same dept list + `dkey`/`deptDisplay` canonicalization as Goals).
- On department select: templated questions render instantly (free), grouped by signal
  type, each with a caption showing the underlying evidence (target, periods, %).
- Auto-fires the polish once per department on open (`fetch('/questions/<dept>')`);
  on success, swaps templated wording for polished wording. Cached server-side, so
  re-opening the same department with unchanged data costs nothing.
- Per-question copy-to-clipboard button; a "Copy all" button emits the department's
  full question list as plain text for pasting into the next QR request.
- Empty state per department: "No gaps flagged for [Dept] this period."

## Data flow

```
/dashboard/data (90s cache, FREE)
  └─ DashboardAggregator.build()
       └─ review_questions: ReviewQuestions.build()   # deterministic SQL
            → D.review_questions in the browser

user opens a department's sheet
  └─ renderQuestions() shows templated questions immediately (FREE)
  └─ fetch('/questions/<dept>')  (on-demand, per department)
       └─ cache hit? → return polished (FREE)
       └─ cache miss? → 1 Haiku call → cache → return polished (~pennies)
```

## Error handling

- Detection failures isolated by `_safe()` — a broken signal doesn't take down the dashboard.
- Phrasing endpoint degrades to templated wording on any LLM/parse error (`polished:false`).
- Unknown/empty department → 200 with empty question list, not an error.
- Phrasing response length mismatch (LLM returns wrong count) → discard polish, fall back to templated.

## Testing

- **Unit (detectors):** `tests/dashboard/test_review_questions.py` using the existing
  `_FakeStore`/`_FakeCursor` substring-mock pattern. Cases per signal: empty data,
  one finding, dept-variant merge (canonical keying), and pace boundary (just inside /
  just outside thresholds).
- **build() shape:** extend the aggregator shape test to assert `review_questions`
  present with `{period, departments}`.
- **Endpoint:** `tests/dashboard/test_questions_route.py` with a mocked Anthropic
  client — assert (a) one call on miss, (b) zero calls on the second identical request
  (cache hit), (c) templated fallback + `polished:false` on client error,
  (d) fallback on count mismatch.
- **Frontend:** extract `<script>`, `node --check`; Jinja `Environment(...).get_template(...)`
  parse check after template edits.

## Success criteria

- Selecting a department shows specific, data-grounded questions for that department.
- Questions cite real values (targets, periods, percentages) from the DB.
- Opening a department triggers at most one Haiku call per distinct findings state;
  repeat opens are free.
- The always-on dashboard build issues zero LLM calls.
- "Copy all" yields text ready to paste into a quarterly-report request.
