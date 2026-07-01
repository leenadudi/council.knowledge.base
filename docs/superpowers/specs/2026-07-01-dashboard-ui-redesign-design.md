# Dashboard UI Redesign (Phase 1) — Design Spec

**Date:** 2026-07-01
**Status:** Draft — pending user review
**Context:** The user designed a polished multi-tab workspace UI in a separate chat (artifact `f3400ddd-…`, full HTML saved at `.claude/projects/.../tool-results/artifact-f3400ddd-1782932929-2828.html`). This spec brings that redesign into the real Flask app, wired to real data where it exists.

---

## 1. Overview

Replace the current single-panel `/dashboard` with the redesigned **multi-tab workspace** from the mockup: a navy sidebar + client-switched views (Dashboard, Departments, Grants, Goals, Projects, Boards & terms), **plus a Resolutions tab** (added per user decision — the mockup omitted it). The visual system (Outfit/Spectral/JetBrains-Mono fonts, refined status palette, KPI cards, tables, CSS Gantt, progress bars) comes straight from the mockup.

**Phase 1 principle (decided with the user):** build the full UI shell and wire the tabs that have **real data**; ship the tabs whose data doesn't exist yet as **polished empty-states** — never fabricated rows. The missing-data domains become separate follow-up projects.

## 2. Data reality (drives what's real vs. empty)

| Tab | Data source | Phase 1 |
|---|---|---|
| **Dashboard** | rollup of the below | KPIs + sections, real where available |
| **Grants** | `grants` (name, dept, status, amount, end_date) | ✅ real; **match/rollover/spent-to-date → "—"** (no such columns) |
| **Departments** | `documents` + `expenditures` + `metrics` | ✅ real (name, spend, report count) |
| **Resolutions** (new tab) | `resolutions` (number, title, status, amount, vendor, adopted_date) | ✅ real |
| **Spending** (dashboard) | `expenditures` | ✅ real (revised budget vs YTD) |
| **Goals** | none (goals live in report narrative, unextracted) | ⛔ empty-state |
| **Projects** (Gantt) | none (graph has names, no dates/builders) | ⛔ empty-state |
| **Boards & terms** | none | ⛔ empty-state |

Empty-states render in the mockup's styling with an honest message, e.g. *"No goals tracked yet — goal extraction from quarterly reports is a planned follow-up."*

## 3. Architecture

- **`templates/dashboard.html`** (replaced) — the mockup's shell: design-system CSS, sidebar, the 7 client-switched `.view` containers, and the existing tab-switching JS (`[data-view]` → toggle `.view.active`/`.nav-item.active`; `.seg` button toggles). Adapted from the saved artifact HTML.
  - **Real panels** are populated by JS from `/dashboard/data` (fetch-then-render, consistent with the current dashboard).
  - **Empty-state tabs** (Goals/Projects/Boards) are static markup.
  - Sidebar **"Ask a question"** and **"Documents"** link to the main app (`/`); they are not new views.
- **`GET /dashboard`** route unchanged (renders `dashboard.html`; 503 when not ready).
- **`GET /dashboard/data`** unchanged route; its aggregator payload is **extended** (below).
- **`src/dashboard/aggregator.py`** — `DashboardAggregator.build()` extended with `departments` and `resolutions` panels and KPI adjustments; existing panels kept.
- **Charting:** the mockup is pure CSS/HTML (Gantt, distribution bars, progress bars). **Drop vis-timeline + Chart.js and their CDN tags** — the redesign is self-contained, no external libraries.

## 4. Aggregator payload (extended `build()`)

```json
{
  "generated_at": "ISO",
  "kpis": {
    "active_grants": int, "grants_expiring_soon": int,
    "grant_funds_active": float,           // SUM(amount) of active grants
    "ytd_spend": float, "revised_budget": float,
    "report_coverage": {"filed": int, "total_departments": int},
    "resolutions_count": int,
    "projects_count": int,                 // distinct project names from graph (count only)
    "goals": null,                         // no data -> UI shows "not tracked"
    "board_seats": null                    // no data
  },
  "grants": [{name, department, status, amount, end_date}],
  "spending_by_dept": [{department, revised_budget, ytd_expended}],
  "departments": [{department, report_count, ytd_expended, revised_budget}],
  "resolutions": [{resolution_number, title, status, amount, vendor, adopted_date}],
  "errors": { "<panel>": "msg" }           // per-panel isolation preserved
}
```

**Query notes:**
- `grants` / `grant_funds_active` / active/expiring: from `grants` (active = status active-ish OR end_date≥today; expiring = active AND end_date within 90d), as today.
- `departments`: distinct `department` from `documents`, joined to summed `expenditures` (revised_budget, ytd_expended) and `documents` report counts. **Derived from SQL, not the graph** (the graph's department list is inflated with name variants).
- `resolutions`: straight select from `resolutions`, ordered by `resolution_number`.
- `projects_count`: count of Project nodes from the graph (best-effort; wrapped so a graph failure just yields null — no timeline data, count only).
- Per-panel `_safe` isolation (existing) retained — a failing panel is `null`/empty + recorded in `errors`; `build()` never raises.

## 5. Template behavior

- On load, `fetch("/dashboard/data")` → populate: Dashboard KPI strip + Grants section, Grants tab table, Departments tab, Resolutions tab, spending figures.
- Goals / Projects / Boards views (and the dashboard's goal/project/board sections) render their **empty-state** markup regardless of payload (no data keys for them).
- Grant table's match/rollover/spent cells render "—" (fields absent).
- Fetch failure or `data.error` → a single inline notice, never a blank page (as today). `data.errors` → note which panels failed, still render the rest.
- Money/number formatting helper; null → "—".

## 6. Error handling

- `/dashboard/data` returns 200 with partial data + `errors` on per-panel failure; 503 when not ready; 500 (logged) on unexpected failure — unchanged from the current route.
- Empty-state tabs never depend on the payload, so they render even if `/dashboard/data` fails.

## 7. Testing

- **Aggregator unit tests** (`tests/dashboard/test_aggregator.py`, extended): with a fake cursor, assert the new `departments` and `resolutions` panels' shape + `grant_funds_active`; assert a raising panel is isolated into `errors` (existing pattern). `projects_count` graph-failure → null.
- **Route tests** (existing) still pass — `/dashboard` 200 HTML, `/dashboard/data` 200 JSON with the documented keys, 503 when not ready.
- **Rendering** (tab switching, real panels populate, empty-states show, no chart-lib references) — verified by running the app against Supabase (manual/integration), as the chart UIs are not unit-tested.

## 8. Files

**Modified**
- `templates/dashboard.html` — replaced with the mockup shell (7 tabs) + JS fetch/populate for real panels + empty-states. Drop vis-timeline/Chart.js CDN tags.
- `src/dashboard/aggregator.py` — add `departments` + `resolutions` panels, `grant_funds_active`, `projects_count`; keep existing panels & `_safe` isolation.
- `tests/dashboard/test_aggregator.py` — cover the new panels.

**Unchanged:** `app.py` routes (`/dashboard`, `/dashboard/data`), the ingestion/query pipelines.

**Source of truth for markup/CSS:** the saved artifact HTML — the implementer adapts its shell, design-system CSS, and per-view structure into the Jinja template, swapping hardcoded rows for JS-populated real data (or empty-states).

## 9. Decisions locked with the user

| Decision | Choice |
|---|---|
| Scope | Shell + real-data panels now; Goals/Projects/Boards = empty-states; missing data = follow-up projects |
| Resolutions | **Add a standalone Resolutions tab** (real data); resolution→grant/project linking is a later project |
| Charts | Pure CSS/HTML per mockup; drop vis-timeline + Chart.js |
| Data delivery | Extend `/dashboard/data`; template fetches + populates (consistent with current dashboard) |
| "Ask a question" / "Documents" | Link to the existing main app, not new views |

## 10. Out of scope / follow-up projects

- **Goals extraction** (quarterly-report "Annual Goals" → a `goals` table) → fills the Goals tab.
- **Project timelines + builders** (needs a source/extraction) → fills the Projects Gantt.
- **Boards & terms** data (needs a source or clerk-maintained roster) → fills the Boards tab.
- **Grant match / rollover / spent-to-date** (needs grant award letters / finance data) → fills those grant columns.
- **Resolution→grant/project/board linking** → lets resolutions weave into those tabs instead of a standalone list.
- **FY selector** wiring (the mockup's FY dropdown is currently cosmetic) — real year-filtering is deferred.

## 11. Open items

- Exact set of KPIs to show on the Dashboard given several have no data — start with active-grants + grant-funds + report-coverage + resolutions-count real, and goals/boards as "not tracked yet."
- Whether the Dashboard gets a small Resolutions summary section in addition to the tab (nice-to-have; default yes if trivial).
