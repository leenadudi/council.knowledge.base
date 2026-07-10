# "Questions for Next Quarter" — Full-Coverage Redesign

**Date:** 2026-07-10
**Status:** Approved (design), pending implementation plan
**Feature owner:** Council KB / ClerkFlow
**Supersedes / extends:** `docs/superpowers/specs/2026-07-09-next-quarter-questions-design.md` (v1)

## Problem

The v1 "Next quarter" tab generates per-department, data-grounded follow-up
questions for the next quarterly report. In practice it only surfaces **a few
departments**, because all three of its signals are *anomaly/gap* signals
(`goal_no_progress`, `goal_stalled`, `budget_pace`). A department shows up only if
it has a goal missing a status, a goal repeated across quarters, or a budget-pace
outlier. Every other department is silent — so the tab reads as sparse and misses
most of the org.

The clerk's ask: proposed questions **for each department**, driven primarily by
previous goals, augmented by other signals already in the database.

## Goal

Broaden generation from 3 gap signals to **5 signal families** so that essentially
every department with any recent data produces useful, prioritized questions —
while keeping detection **100% deterministic and free** (per the standing
"ask before LLM spend" rule), and moving the single optional LLM call from
polish-only to **polish + cross-table synthesis**, still on-demand and cached.

## Scope

**In scope**
- Broadened deterministic detection covering: goals of *all* statuses, vacancies,
  grants, quiet (behind-filing) departments, and the existing budget-pace signal.
- A `priority` field on every finding; ranking + soft cap (top ~8, "show all N").
- Department picker lists the full canonical department union, not just gap depts.
- On-demand, cached LLM pass upgraded to polish **and** synthesize 2–3 cross-cutting
  questions per department.

**Out of scope (this iteration)**
- Metrics-based signals (metrics remain over-extracted/noisy — see strategic memory).
- Persistent cross-restart cache table (in-memory module cache only, as v1).
- Editing/saving questions back into the app; export beyond clipboard.
- A generic "any updates?" filler for departments with zero real signals — omitted
  deliberately (it is noise). Such departments show the empty state.

## Cost constraint (hard requirement, unchanged)

The LLM pass **must never run inside the always-on dashboard build**
(`DashboardAggregator.build()` / `/dashboard/data`, 90s cache). Detection is 100%
deterministic SQL and carries no cost. The LLM call runs only on demand, per
department, when a department's sheet is opened, and caches keyed to the underlying
findings so identical data never re-triggers a call.

## Architecture

Same split as v1 — free deterministic detector + on-demand cached LLM pass — with
broadened content on both sides.

### 1. Detection module — `src/dashboard/review_questions.py`

`ReviewQuestions.build()` continues to return
`{ "period": <label|None>, "departments": [ {department, findings:[...]} ] }`,
keyed by canonical department (`DashboardAggregator._dept_key` / `_dept_display`),
sorted by department name. It remains wired into `DashboardAggregator.build()` as
the `"review_questions"` panel via `_safe()` (deterministic, no LLM).

**Finding shape (adds `priority`):**
```
{
  "signal": "goal_no_progress" | "goal_stalled" | "goal_in_progress"
          | "goal_completed" | "vacancy" | "grant" | "quiet_department"
          | "budget_pace" | "synthesis",
  "department": "<display name>",
  "question": "<templated question string>",   # free fallback wording
  "priority": "highest" | "high" | "medium" | "low",
  "evidence": { ... signal-specific raw fields ... }
}
```
`priority` drives ranking (§3). `synthesis` findings are produced only by the LLM
pass (§4), never by `build()`.

### 2. Signal families

Let `latest` = the newest `(year, quarter)` present in `goals` (the reporting
period the sheet is prepped against), and `eff(goal) = user_status or status`,
trimmed.

**A. Goals — all statuses** (primary coverage driver). Group goal rows by
`(dept_key, normalized_title)` across periods, take the latest row per title:

| Condition | signal | priority | Template |
|---|---|---|---|
| same title across ≥2 distinct periods, `eff` empty throughout | `goal_stalled` | highest | *"'[title]' has appeared in [N] quarterly reports ([first]→[last]) with no status update — what progress has been made since [last]?"* |
| latest period, `eff` empty | `goal_no_progress` | high | *"[Dept]'s goal '[title]'[ (target: X)] has no progress reported for [period]. What's the current status?"* |
| `eff` == in-progress-like | `goal_in_progress` | medium | *"Last quarter [Dept]'s goal '[title]'[ (target: X)] was in progress. What progress was made this quarter?"* |
| `eff` == completed-like | `goal_completed` | low | *"[Dept] reported '[title]' complete. What's the follow-on objective for next quarter?"* |

Status classification is tolerant string matching (e.g. contains "complete"/"done"
→ completed; contains "progress"/"ongoing"/"underway"/"in progress" → in-progress;
anything else non-empty → treated as in-progress for wording). Stalled remains the
sharpest gap and suppresses a redundant `goal_no_progress` for the same title.

**Behavior change vs v1 — clerk-set status demotes, does not suppress.** In v1, a
clerk setting `user_status` removed the goal's question. Now a clerk-set status
routes the goal to the `goal_in_progress` / `goal_completed` branch (low/medium
priority, foldable) rather than removing it — the clerk still receives a
forward-looking next-quarter prompt. Approved 2026-07-10.

**B. Vacancies** — `signal: "vacancy"`, priority **medium**. Latest period per
department, rows with `status = 'open'`. Aggregate a department's open titles into
one finding:
*"[Dept] reported [total_open or 'open'] vacanc(y/ies) in [period] ([title (n),
title (n), …]). What's the current hiring status?"*
Evidence: `{period, positions:[{title, count}], total_open}`.

**C. Grants** — `signal: "grant"`, priority **medium-low**. Grants that are
active/recent: `status` not clearly closed AND (`end_date` is null or ≥ start of
`latest` year), one finding per grant (cap a department's grant findings, see §3):
*"Grant '[grant_name]'[ ([$amount])] was [status]. What's the current status /
drawdown for next quarter?"*
Evidence: `{grant_name, grant_number, amount, status, end_date}`.

**D. Quiet department** — `signal: "quiet_department"`, priority **high**.
Canonical department universe = union of `dept_key` across `goals`,
`expenditures`, `grants`, `vacancies`, and `documents`. For each department, find
its newest `documents` row with `document_type = 'quarterly_report'`. If that
period is older than the overall latest quarterly-report period (or missing),
emit:
*"[Dept] hasn't filed a quarterly report since [last_filed | 'the period on
record'] — please provide a [current-quarter] update."*
Evidence: `{last_filed_period, latest_period}`. This guarantees behind-filing
departments surface even when they have no goals/budget/vacancy/grant rows.

**E. Budget pace** (existing, unchanged) — `signal: "budget_pace"`, priority
**high**. Per department (latest expenditure period, excluding `%total%` line
items), `pace = ytd_expended / revised_budget` vs expected quarterly pace
(Q1 .25 → Q4 1.0). Flag `pace > 1.5×` (ahead) or `< 0.5×` (behind), requiring
`revised_budget > 0`.

**Empty state.** A department that is current on filings and has no goals,
vacancies, active grants, or budget anomaly produces no findings and renders the
per-department empty state. No filler question is generated.

### 3. Ranking + soft cap

Within each department, sort findings by priority
(`highest > high > medium > low`), then group under signal headings in a stable
order (gaps first). Render the **top ~8** findings by default; if more exist, fold
the remainder behind a **"show all N"** expander. Nothing is discarded — only
folded. Per-signal internal caps prevent one noisy signal from crowding out others
(e.g. cap grants at ~5 per department before folding).

### 4. On-demand LLM pass — `/questions/<dept>` (polish + synthesis)

Explicit, per-department, cached; still never called from `build()`.
1. Run `ReviewQuestions.build()` (deterministic SQL, cheap); select the target
   department's findings. Fresh detection pass, independent of the dashboard cache.
2. Compute a stable hash of that department's findings list (cache key).
3. On cache miss, issue **one** LLM call (`settings.profiler_model` / Haiku) that:
   - **Polishes** every templated question into natural, clerk-ready wording,
     preserving every number, percentage, target, goal name, and department name
     exactly (existing `_PHRASE_SYSTEM` contract).
   - **Synthesizes** up to **3 cross-cutting questions** from a compact,
     department-scoped snapshot (goals + budget pace + vacancies + grants counts)
     passed alongside — e.g. *"Vacancies rose while goal '[X]' stalled — is
     staffing the blocker?"* Synthesis questions must be grounded only in the
     supplied facts (no invented numbers) and are returned tagged so the client
     renders them under a "Cross-cutting" heading with `signal: "synthesis"`.
   - Returns JSON: `{ "polished": [<same length/order as input>],
     "synthesis": [<0–3 strings>] }`.
4. Cache `{dept_hash → {polished, synthesis}}` in-memory (module-level dict).
5. Response: `{ department, questions:[{question, signal, priority, evidence}...],
   synthesis:[...], polished: true|false }`.

Graceful degradation: any LLM/parse error, or a `polished` length mismatch →
return templated wording with `polished:false` and no synthesis. Unknown/empty
department → 200 with empty lists.

### 5. Frontend — `templates/redesign.html` (`renderQuestions()`)

- Department picker (`#q-dept`) lists the **full canonical department union**
  present in `D.review_questions` (every department with findings), plus retains
  the "All" option.
- Findings render grouped by signal heading, ordered by priority, with the soft
  cap + "show all N" expander (§3). Each question keeps its evidence caption
  (target, periods, %, counts).
- On department select, fire `fetch('/questions/<dept>')` once; on success swap
  templated wording for polished wording and append `synthesis` questions under a
  "Cross-cutting" heading. Client-cached (`_qPolished`); server-cached by hash.
- Per-question copy button + "Copy all" (plain text, ready to paste into a QR
  request). "Copy all" includes synthesis questions when present.
- Per-department empty state: "No items flagged for [Dept] this period."
- Goal-status change still invalidates `_dashboard_cache` so the sheet refreshes
  (a demoted goal moves to the low-priority section on next build).

## Data flow

```
/dashboard/data (90s cache, FREE)
  └─ DashboardAggregator.build()
       └─ review_questions: ReviewQuestions.build()   # deterministic SQL, 5 signal families
            → D.review_questions in the browser (all departments)

user opens a department's sheet
  └─ renderQuestions() shows templated, ranked, capped questions immediately (FREE)
  └─ fetch('/questions/<dept>')  (on-demand, per department, explicit)
       └─ cache hit?  → return polished + synthesis (FREE)
       └─ cache miss? → 1 Haiku call → cache → polished + synthesis (~pennies)
```

## Error handling

- Each signal detector is isolated; a broken signal must not take down `build()`
  (wrapped by the aggregator's `_safe()` and defensive per-signal try/except).
- LLM pass degrades to templated wording on any error (`polished:false`,
  no synthesis).
- Length-mismatch on the polished array → discard polish, fall back to templated.
- Unknown/empty department → 200 with empty lists, not an error.

## Testing

- **Unit (detectors)** in `tests/dashboard/test_review_questions.py` using the
  existing `_FakeStore`/`_FakeCursor` substring-mock pattern:
  - goals: each status branch (no-progress, stalled, in-progress, completed),
    clerk-set status → demotion (not suppression), dept-variant canonical merge.
  - vacancies: aggregation of multiple open titles into one finding; filled-only
    dept → none.
  - grants: active vs closed/expired filtering; per-dept grant cap.
  - quiet department: behind-filing dept emitted; current dept not emitted; dept
    with no documents row handled.
  - budget pace: boundary cases (just inside / just outside thresholds) — retained.
  - ranking + soft cap: priority ordering; >8 findings fold; per-signal cap.
- **build() shape:** assert `review_questions` present with `{period, departments}`
  and that findings carry `priority`.
- **Endpoint** (`tests/dashboard/test_questions_route.py`, mocked Anthropic):
  (a) one call on miss returning polished + synthesis; (b) zero calls on identical
  repeat (cache hit); (c) templated fallback + `polished:false` on client error;
  (d) fallback on polished length mismatch; (e) synthesis absent on error.
- **Frontend:** extract `<script>`, `node --check`; Jinja
  `Environment(...).get_template(...)` parse check after template edits.

## Success criteria

- Essentially every department with recent data shows prioritized, data-grounded
  questions; behind-filing departments surface via the quiet signal.
- Previous goals of every status generate forward-looking questions (gaps ranked
  above routine follow-ups).
- Each department's sheet stays scannable (top ~8, rest folded).
- Opening a department triggers at most one Haiku call per distinct findings state;
  repeat opens are free; the always-on dashboard build issues zero LLM calls.
- The on-demand pass adds cross-cutting synthesis questions grounded in real data.
- "Copy all" yields paste-ready text for the next quarterly-report request.
