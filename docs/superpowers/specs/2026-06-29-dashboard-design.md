# Government Timeline Dashboard — Design Spec

**Date:** 2026-06-29
**Status:** Draft — pending user review
**Context:** Theme #5 from the Harrisburg clerk meeting (`harrisburg_clerk_meeting_2026-06-29_buildnotes.md`) — a visual aid that surfaces the important timeline information at a glance. Builds on the agentic-ingestion work already merged (registry, resolutions schema, etc.).

---

## 1. Overview

A new in-app page, `/dashboard`, that gives the clerk and staff a single visual view of the city's timeline information drawn from the knowledge base: grant lifecycles, report cadence, spending over time, and (once resolution documents are ingested) council authorizations. Hybrid layout: a KPI strip on top, an interactive timeline as the centerpiece, and detail tables below.

**Audience:** internal clerk/staff. No authentication (consistent with the current app, which has none).

**Data reality:** the dashboard is built only from data that exists today — `documents`, `grants`, `expenditures`, `metrics`, `vacancies`. The `resolutions`/`votes` tables exist but are empty until resolution PDFs are ingested; the dashboard's resolution lane and KPI are designed to light up automatically when that data arrives, and to render an empty state until then.

## 2. Architecture

Clean three-way separation (data / wiring / presentation):

- **`src/dashboard/aggregator.py`** (new) — `DashboardAggregator(sql_store)` with a `build() -> dict` method that runs the read queries and returns the full dashboard payload. All dashboard SQL lives here. Each panel is built in its own method wrapped in try/except so one failing query doesn't blank the page.
- **`app.py`** — two routes:
  - `GET /dashboard` → `render_template("dashboard.html")` (static shell). Returns 503 when `_ready` is false (matches existing routes).
  - `GET /dashboard/data` → `jsonify(DashboardAggregator(_sql_store).build())`. Returns 503 when not ready; otherwise 200 with partial data + per-panel error flags.
  - A nav link added to `templates/index.html` pointing at `/dashboard`.
- **`templates/dashboard.html`** (new) — fetches `/dashboard/data` on load and renders the KPI strip, timeline, and tables. Uses vis-timeline + Chart.js via CDN.

## 3. Data payload & panels

`build()` returns:

```json
{
  "generated_at": "ISO-8601",
  "kpis": {
    "active_grants": int, "grants_expiring_soon": int,
    "ytd_spend": float, "revised_budget": float,
    "latest_period": {"year": int, "quarter": "Q1"},
    "report_coverage": {"filed": int, "total_departments": int},
    "resolutions_count": int,
    "unclassified_docs": int
  },
  "timeline": {
    "grants":      [{"id","label":grant_name,"department","start":date,"end":date,"status","amount"}],
    "reports":     [{"id","department","date":quarter_start,"quarter","year","document_type"}],
    "resolutions": [{"id","label","date":adopted_date,"amount","status"}],
    "spending":    [{"period":"YYYY Qn","ytd_expended":float}]
  },
  "tables": {
    "grants":   [...], "spending_by_dept": [...], "reports": [...]
  },
  "errors": { "<panel>": "message" }   // present only if a panel failed
}
```

**Query definitions (concrete):**

- **active_grants / expiring:** from `grants` — active = `status ILIKE any('active','in_progress','open','pending')` OR `end_date >= current_date`; expiring_soon = active AND `end_date` within 90 days of `current_date`.
- **ytd_spend / revised_budget:** `SUM(ytd_expended)`, `SUM(revised_budget)` from `expenditures` for the latest year.
- **latest_period:** `MAX(year)` from `documents`, then `MAX(quarter)` within that year.
- **report_coverage:** distinct departments with a `document_type='quarterly_report'` row in the latest period ÷ distinct departments across all `documents`.
- **resolutions_count:** `COUNT(*)` from `resolutions` (0 today).
- **unclassified_docs:** `COUNT(*)` from `documents WHERE document_type='unclassified'` (the quarantine signal from ingestion).
- **timeline.grants:** `grants` rows with non-null `start_date` (end defaults to start+1yr if null, flagged).
- **timeline.reports:** `documents` rows mapped to a marker date via quarter→month (Q1→Jan 1, Q2→Apr 1, Q3→Jul 1, Q4→Oct 1) of `year`.
- **timeline.resolutions:** `resolutions` rows by `adopted_date` (empty today).
- **timeline.spending:** `SUM(ytd_expended)` grouped by `(year, quarter)`, ordered chronologically.
- **tables:** straightforward selects (grants; expenditures grouped by department for latest year; documents ordered by year/quarter desc).

Dates use timezone-aware `datetime.now(timezone.utc)` (avoids the deprecated `utcnow` pattern).

## 4. Presentation (CDN libraries)

- **vis-timeline** (pinned version, CDN): renders `timeline.grants` as **range items**, `timeline.reports` and `timeline.resolutions` as **point items**, in three **groups/lanes** (Grants, Reports, Resolutions). Zoom/pan enabled. Grant items colored by status.
- **Chart.js** (pinned version, CDN): renders `timeline.spending` as a bar chart, plus optional KPI mini-charts.
- KPI strip is plain HTML/CSS cards. Detail tables are plain HTML tables.
- **Empty states:** any lane/table with no rows renders a muted "No <thing> yet" message rather than an empty widget. Resolutions lane always shows this until ingested.
- Visual style matches `index.html` (same fonts/colors/spacing).
- **Hardening note (follow-up, not now):** the two CDN `<script>`/`<link>` tags can later be vendored into `static/` for offline use / no external calls. Defaulting to CDN per user.

## 5. Error handling

- `/dashboard/data` returns **200 with partial data** when individual panels fail — each failure is caught in its aggregator method, that panel's value is `null`/empty, and a message is added under `errors`. The page renders the panels it can and shows a small inline notice for any in `errors`.
- Full not-ready / store-unreachable → **503** (consistent with `/ask`, `/departments`).
- The page handles a failed `/dashboard/data` fetch by showing a single "Dashboard data unavailable" banner, never a blank screen.

## 6. Testing

- **Aggregator unit tests** (`tests/dashboard/test_aggregator.py`) — inject a **fake cursor** returning canned rows; assert shaping logic: quarter→marker-date mapping, coverage ratio, expiring-soon window, empty-resolutions path, and that a raising panel is caught and recorded in `errors` (not propagated).
- **Route test** (`tests/dashboard/test_dashboard_route.py`) — stub the aggregator; assert `GET /dashboard` returns 200 HTML and `GET /dashboard/data` returns 200 JSON with the documented top-level keys; assert 503 when `_ready` is false.
- **Live DB** assertions marked `@pytest.mark.integration` (won't run without Supabase), same convention as the ingestion work.
- **Rendering** (vis-timeline/Chart.js drawing) is verified by running the app and viewing `/dashboard` — charting UIs are not meaningfully unit-tested.

## 7. Files

**New**
- `src/dashboard/__init__.py`, `src/dashboard/aggregator.py`
- `templates/dashboard.html`
- `tests/dashboard/__init__.py`, `tests/dashboard/test_aggregator.py`, `tests/dashboard/test_dashboard_route.py`

**Modified**
- `app.py` — add `/dashboard` + `/dashboard/data` routes.
- `templates/index.html` — add a nav link to `/dashboard`.

## 8. Decisions locked with the user

| Decision | Choice |
|---|---|
| Form | Live in-app `/dashboard` route (live Supabase data) |
| Layout | Hybrid: KPI strip + timeline hero + detail tables |
| Audience / auth | Internal clerk/staff, no auth |
| Timeline library | vis-timeline (ranges + point markers + lanes) |
| Charts | Chart.js (spending bars / KPI minis) |
| Library delivery | CDN, pinned versions (vendoring noted as follow-up) |

## 9. Open items (decide during implementation)

- Exact pinned CDN versions of vis-timeline and Chart.js.
- "Active grant" status vocabulary may need tuning once real grant `status` values are observed in the data.
- Whether to add a department filter to the timeline (deferred unless trivial — YAGNI for v1).
