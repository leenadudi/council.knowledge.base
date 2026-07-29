# Year-to-Date Reporting — Design

Date: 2026-07-29
Status: approved direction from clerk meeting 2026-07-29 (YTD reporting was priority #1)

## Problem

Every quantitative view on the dashboard is a single-period snapshot (latest
quarter). The clerk's most-requested capability is a year-scoped view of council
activity: what has Council done *so far this year*, aggregated across quarters —
the numbers a clerk carries into a council briefing or an annual report draft.

## Questions the feature answers (clerk audience)

1. **Legislative output** — How many resolutions and ordinances has Council
   adopted this year? How many sessions has it held? How does activity break
   down quarter by quarter? How much money has Council authorized YTD?
2. **Spending vs budget** — Where does citywide spending stand against the
   revised budget, and is the pace ahead of or behind what you'd expect at this
   point in the year (≈25% per elapsed quarter)? Which departments are running
   hot? How did spending accumulate quarter over quarter?
3. **Grants** — What new grant awards started this year, worth how much? How
   many grants are active overall?
4. **Goal progress** — Of the department goals set this year, how many are
   completed / in progress / not yet started?
5. **Honesty about coverage** — every number is labeled "through Qn" and the
   partial-data disclaimer applies; the clerk shares these numbers publicly.

Non-goals (YAGNI): multi-year comparison charts, CSV export, per-vendor YTD,
PDF generation (browser print covers it), any LLM involvement.

## Design

### Data semantics

- `expenditures.ytd_expended` is already cumulative within a year. YTD spend
  for year Y = the snapshot at the **latest quarter of Y** (not a SUM across
  quarters, which double-counts). Quarter-over-quarter increments = successive
  differences between snapshots.
- Resolutions/ordinances are counted by `adopted_date` calendar year, from the
  `resolutions` and `legislation` tables (consistent with the rest of the
  dashboard). Rows without `adopted_date` are excluded from YTD counts (they
  can't be placed in a year) — the view says how many were excluded.
- Meetings counted by `meeting_date` year.
- "New grants this year" = deduped grants (reusing `_dedupe_grants`) with
  `start_date` in the selected year. Active count reuses the KPI liveness rule.
- Goals for the selected year, bucketed by `user_status` (clerk override wins)
  else keyword inference on `status` (same `_STATUS_COMPLETE` convention as
  `review_questions.py`).
- Expected spending pace = elapsed quarters ÷ 4 (elapsed = index of latest
  quarter with expenditure data in the year).

### Backend

New module `src/dashboard/ytd.py` — class `YTDReport(sql_store)`, mirroring the
`ReviewQuestions` / `Projects` pattern (aggregator delegates via a lazy import;
`aggregator.py` stays flat). Reuses `DashboardAggregator._dedupe_grants`,
`_dept_key`, `_dept_display`.

`DashboardAggregator.build()` gains a `"ytd"` panel (wrapped in `_safe` like
every other panel; zero LLM calls; served through the existing cached
`/dashboard/data` route — no new endpoint).

Payload:

```
ytd: {
  years: [2026, 2025, ...],          # years with any data, desc — drives the year selector
  reports: { "<year>": {
    year, as_of_quarter,             # latest quarter in year with any data
    quarters_elapsed,                # 1..4, from as_of_quarter
    legislative: {
      resolutions_adopted, ordinances_adopted, meetings_held,
      council_authorized,            # SUM(resolutions.amount) for the year
      undated_items,                 # rows excluded for missing adopted_date
      by_quarter: [{quarter, resolutions, ordinances, meetings, authorized}],
    },
    spending: {
      ytd_expended, revised_budget, pct_used, expected_pct,
      by_quarter: [{quarter, ytd_expended, increment}],
      by_department: [{department, revised_budget, ytd_expended, pct}],
    },
    grants: {
      new_count, new_amount, active_count,
      new_grants: [{grant_name, department, amount, start_date, status}],
    },
    goals: { total, completed, in_progress, not_started },
  }}
}
```

All years are precomputed in one build (the data set is small and the payload
is cached 90s); the frontend year selector is a pure client-side switch.

### SQL schema

No new tables — every fact needed already lands in SQL. One dated migration,
`sql/migrate_2026_07_29_ytd_indexes.sql`: indexes on
`resolutions(adopted_date)`, `legislation(adopted_date)`, and
`grants(start_date)` to back the year-bucketed scans.

### Frontend (`templates/redesign.html`)

New "Year to date" nav item under Explore → view `v-ytd`, rendered by
`renderYTD()` from `D.ytd` (same render-on-tab pattern as other views):

- Header with year selector (only when >1 year) and a Print button
  (`window.print()`); "through Qn" as-of label on the page subtitle.
- KPI row: resolutions adopted · ordinances adopted · sessions held ·
  council-authorized $ · budget used % (vs expected pace).
- **Quarter-by-quarter table**: one row per quarter with data — resolutions,
  ordinances, meetings, authorized $, spending increment, cumulative YTD spend.
- **Spending vs budget by department**: budget / YTD spent / % bar, flagged
  when % used exceeds expected pace.
- **Grants received this year**: list of new grants + new-award total; active
  count in the KPI sub-line.
- **Goal progress**: completed / in progress / not started summary bar.
- A `@media print` rule hides the sidebar/header chrome so the view prints as
  a clean one-page report.

### Error handling

Panel failure degrades to `ytd: null` (existing `_safe` isolation) and the tab
shows an empty-state message. Zero-data years simply don't appear in `years`.

### Testing

`tests/dashboard/test_ytd.py` with the `_FakeStore` canned-SQL pattern from
`test_aggregator.py` (no live DB; not marked integration): YTD-spend picks the
latest-quarter snapshot rather than summing; quarterly increments; adopted-date
year bucketing incl. undated exclusions; grant new-vs-active; goal bucketing
with `user_status` override; empty-DB shape.
