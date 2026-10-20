# Dashboard UI Redesign (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-panel `/dashboard` with the mockup's multi-tab workspace (Dashboard, Departments, Grants, Resolutions, Goals, Projects, Boards), wiring the tabs that have real data and shipping honest empty-states for the rest.

**Architecture:** Extend `DashboardAggregator.build()` with `departments` and `resolutions` panels + a `grant_funds_active` KPI. Rebuild `templates/dashboard.html` from the saved mockup (design system + sidebar + 7 client-switched views + tab-switching JS), pure CSS (no chart libraries). Real panels are populated by a `fetch("/dashboard/data")` + render script; Goals/Projects/Boards are static empty-states.

**Tech Stack:** Python 3.11+, Flask/Jinja, psycopg2 (RealDictCursor → dict rows), pytest; vanilla JS + CSS in the template (no vis-timeline, no Chart.js).

**Mockup source of truth:** `/Users/leenadudi/.claude/projects/-Users-leenadudi-council-knowledge-base/dbb99f7a-6e4d-4dc9-b237-43e98c577acc/tool-results/artifact-f3400ddd-1782932929-2828.html` (1320 lines: design-system CSS in `<style>`, `.shell` sidebar, 6 `.view` containers at lines 391/763/892/972/1080/1152, tab-switch JS at 1291). The implementer adapts this file.

## Global Constraints

- **No chart libraries** — the mockup's visuals (Gantt, distribution/progress bars, KPI cards) are pure CSS/HTML. Do NOT add or keep vis-timeline / Chart.js or any CDN `<script>`/`<link>`.
- **`parser_used`/pipeline untouched** — this is a read-only presentation change plus aggregator reads. No writes to any store.
- `SQLStore.cursor()` yields a **RealDictCursor** (dict rows). Parameterized SQL only.
- **Never fabricate data** — tabs without a data source (Goals, Projects, Boards) render styled empty-states, not sample rows. The grant match/rollover/spent-to-date cells render `—`.
- **Per-panel error isolation preserved** — new aggregator panels go through the existing `_safe(name, fn, errors)`; `build()` never raises.
- `/dashboard` and `/dashboard/data` routes and their 503-when-not-ready behavior are unchanged.
- Sidebar **"Ask a question"** and **"Documents"** items link to the main app (`href="/"`), not new views.
- Tests: `pytest`. Live-DB / browser rendering is an operator step, not CI.

---

### Task 1: Extend the aggregator (departments + resolutions panels, grant-funds KPI)

**Files:**
- Modify: `src/dashboard/aggregator.py`
- Test: `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Consumes: existing `DashboardAggregator(sql_store, now=None)`, `_safe`, `_build_kpis`.
- Produces:
  - `_build_kpis()` gains `grant_funds_active` (float; SUM of active-grant amounts).
  - `_build_departments() -> list[dict]` — `{department, revised_budget, ytd_expended, report_count}`.
  - `_build_resolutions() -> list[dict]` — `{resolution_number, title, status, amount, vendor, adopted_date}` (amount→float, adopted_date→ISO).
  - `build()` output gains top-level `"departments"` and `"resolutions"` (each via `_safe`). Existing `kpis`/`timeline`/`tables` keys stay (timeline is now unused by the UI but retained to avoid churn).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/dashboard/test_aggregator.py
def test_build_departments_shape():
    store = _FakeStore({
        "DISTINCT department": [{"department": "Codes"}, {"department": "Fire"}],
        "FROM expenditures GROUP BY department": [{"department": "Codes", "rb": 100.0, "ytd": 40.0}],
        "quarterly_report' GROUP BY": [{"department": "Codes", "c": 4}],
    })
    depts = DashboardAggregator(store)._build_departments()
    codes = next(d for d in depts if d["department"] == "Codes")
    assert codes["revised_budget"] == 100.0 and codes["ytd_expended"] == 40.0 and codes["report_count"] == 4
    fire = next(d for d in depts if d["department"] == "Fire")
    assert fire["revised_budget"] == 0 and fire["report_count"] == 0   # no rows -> zeros

def test_build_resolutions_shape():
    import datetime
    store = _FakeStore({"FROM resolutions ORDER BY": [
        {"resolution_number": "9-2026", "title": "BUILD Grant", "status": "Passed",
         "amount": 3000000.0, "vendor": "USDOT", "adopted_date": datetime.date(2026,1,27)}]})
    r = DashboardAggregator(store)._build_resolutions()[0]
    assert r["resolution_number"] == "9-2026" and r["amount"] == 3000000.0
    assert r["adopted_date"] == "2026-01-27"    # ISO string

def test_build_includes_new_panels():
    store = _FakeStore({
        "FROM grants WHERE": [{"active": 0, "expiring": 0}],
        "FROM grants": [{"funds": 0}],
        "FROM expenditures": [{"ytd": 0, "budget": 0}],
        "MAX(year)": [{"year": None}], "FROM resolutions": [],
        "document_type='unclassified'": [{"c": 0}],
        "DISTINCT department": [], "GROUP BY department": [], "quarterly_report' GROUP BY": [],
        "GROUP BY year, quarter": [], "FROM documents ORDER BY": [],
    })
    out = DashboardAggregator(store).build()
    assert "departments" in out and "resolutions" in out
    assert "grant_funds_active" in out["kpis"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/dashboard/test_aggregator.py -k "departments or resolutions or new_panels" -v`
Expected: FAIL (`_build_departments` undefined; `grant_funds_active` missing)

- [ ] **Step 3: Implement in `src/dashboard/aggregator.py`**

In `_build_kpis`, add a grant-funds query (near the existing grants query) and include it in the returned dict. Add after the existing active/expiring `grants` query:

```python
            cur.execute(
                "SELECT COALESCE(SUM(amount),0) AS funds FROM grants "
                "WHERE (LOWER(status) = ANY(%s) OR end_date >= %s)",
                (statuses, today),
            )
            gf = cur.fetchone() or {}
```

and add to the returned kpis dict: `"grant_funds_active": float(gf.get("funds", 0) or 0),`

Add two methods:

```python
    def _build_departments(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT DISTINCT department FROM documents "
                        "WHERE department IS NOT NULL AND department <> '' ORDER BY department")
            depts = [r["department"] for r in cur.fetchall()]
            cur.execute("SELECT department, COALESCE(SUM(revised_budget),0) AS rb, "
                        "COALESCE(SUM(ytd_expended),0) AS ytd FROM expenditures GROUP BY department")
            spend = {r["department"]: r for r in cur.fetchall()}
            cur.execute("SELECT department, COUNT(*) AS c FROM documents "
                        "WHERE document_type='quarterly_report' GROUP BY department")
            reports = {r["department"]: r["c"] for r in cur.fetchall()}
        out = []
        for d in depts:
            sp = spend.get(d) or {}
            out.append({
                "department": d,
                "revised_budget": float(sp.get("rb") or 0),
                "ytd_expended": float(sp.get("ytd") or 0),
                "report_count": int(reports.get(d, 0) or 0),
            })
        return out

    def _build_resolutions(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT resolution_number, title, status, amount, vendor, adopted_date "
                        "FROM resolutions ORDER BY resolution_number")
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("amount") is not None:
                r["amount"] = float(r["amount"])
            if hasattr(r.get("adopted_date"), "isoformat"):
                r["adopted_date"] = r["adopted_date"].isoformat()
        return rows
```

In `build()`, add the two panels:

```python
            "departments": self._safe("departments", self._build_departments, errors),
            "resolutions": self._safe("resolutions", self._build_resolutions, errors),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/dashboard/test_aggregator.py -v`
Expected: PASS (existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): aggregator departments + resolutions panels + grant-funds KPI"
```

---

### Task 2: New multi-tab template shell (design system, 7 views, empty-states, tab JS)

**Files:**
- Modify (replace): `templates/dashboard.html`
- Test: `tests/dashboard/test_dashboard_route.py` (extend)

**Interfaces:**
- Consumes: nothing at runtime yet (data-binding JS is Task 3).
- Produces: a `/dashboard` page with the mockup shell — sidebar with 7 `data-view` nav items (dashboard, departments, grants, resolutions, goals, projects, boards) + "Ask a question"/"Documents" linking to `/`; 7 `.view` containers with matching ids (`view-dashboard`, …, `view-resolutions`); tab-switch JS. Real-panel regions carry stable ids for Task 3 to populate; Goals/Projects/Boards are empty-states.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/dashboard/test_dashboard_route.py
def test_dashboard_has_all_tabs(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", True)
    html = _client().get("/dashboard").data.decode()
    for view in ("view-dashboard","view-departments","view-grants","view-resolutions",
                 "view-goals","view-projects","view-boards"):
        assert f'id="{view}"' in html
    # empty-states present for data-less tabs
    assert "no goals tracked yet" in html.lower()
    # no chart libraries
    assert "vis-timeline" not in html.lower() and "chart.js" not in html.lower() and "chart.umd" not in html.lower()
    # ask/documents link to main app
    assert 'href="/"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/dashboard/test_dashboard_route.py -k tabs -v`
Expected: FAIL (current template has none of these views)

- [ ] **Step 3: Build `templates/dashboard.html` from the mockup**

Start from the mockup source-of-truth file (path in the header). Copy its `<style>` design system and `.shell` sidebar + `.main` structure verbatim into `templates/dashboard.html` (this is a Flask template — write the page body directly; no `{{ }}` needed yet). Then apply these exact transformations:

1. **Sidebar nav:** keep the 6 mockup `data-view` items; **add a 7th** before Goals: `<a class="nav-item" data-view="resolutions"><span class="dot"></span> Resolutions</a>`. Give **"Ask a question"** and **"Documents"** real `href="/"` (they already have `title="Lives in the main app"`).
2. **Add a `#view-resolutions` view** (a `.page.view` container) modeled on the grants view: a page-head ("Resolutions" / "Council authorizations — status, amount, vendor"), and a `.panel` with an empty `<table class="data" id="resolutions-table">` (thead: Resolution, Title, Status, Amount, Vendor, Adopted) whose `<tbody id="resolutions-body">` is empty (Task 3 fills it).
3. **Real-panel regions** — give these empty containers stable ids for Task 3 (leave them empty, NOT the mockup's hardcoded rows):
   - Dashboard KPI strip → `id="dash-kpis"` (empty).
   - Dashboard grants section table body → `id="dash-grants-body"`.
   - Grants tab table body → `id="grants-body"`; its KPI strip → `id="grants-kpis"`.
   - Departments tab cards container → `id="departments-cards"`; its KPI strip → `id="dept-kpis"`.
   - Resolutions tab → `id="resolutions-body"` (from #2).
   Remove the mockup's hardcoded `<tr>`/card sample rows inside these regions.
4. **Empty-state tabs** — Goals (`view-goals`), Projects (`view-projects`), Boards (`view-boards`): keep the mockup's page-head + panel chrome, but replace the sample content with a styled empty-state. Add a small CSS class if useful, e.g. reuse the existing `.empty` class. Exact copy:
   - Goals: `<div class="empty">No goals tracked yet — goal extraction from quarterly reports is a planned follow-up.</div>`
   - Projects: `<div class="empty">No project timelines yet — project dates &amp; builders aren't extracted yet (planned follow-up).</div>`
   - Boards: `<div class="empty">No board/term data yet — board appointments &amp; terms aren't ingested yet (planned follow-up).</div>`
5. **Drop chart libraries:** ensure NO `vis-timeline`/`Chart.js`/CDN `<script>`/`<link>` tags exist (the mockup has none — just confirm none are added).
6. **Keep the mockup's tab-switch + segmented-control JS** (the `(function(){…data-view…})()` block at the end). Do NOT add data-fetching yet (Task 3).

Keep the mockup's dashboard sections for grants (real, populated in Task 3) and the goals/projects/boards sections as inline empty-states too (so the dashboard rollup doesn't show fake goals/projects). For the dashboard's project Gantt + board-terms + goal rows, replace sample content with the same `.empty` messages.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/dashboard/test_dashboard_route.py -v`
Expected: PASS (existing route tests + the new tabs test). Then `pytest -q -m "not integration"` — full suite green.

- [ ] **Step 5: Commit**

```bash
git add templates/dashboard.html tests/dashboard/test_dashboard_route.py
git commit -m "feat(dashboard): multi-tab workspace shell + Resolutions tab + empty-states"
```

---

### Task 3: Data-binding JS — populate real panels from /dashboard/data

**Files:**
- Modify: `templates/dashboard.html` (add a data-loading script)
- (verification only; no new unit test — JS rendering is verified by running the app)

**Interfaces:**
- Consumes: `GET /dashboard/data` payload from Task 1 — `kpis` (active_grants, grants_expiring_soon, grant_funds_active, ytd_spend, revised_budget, report_coverage{filed,total_departments}, resolutions_count, unclassified_docs), `tables.grants` ([{name,department,amount,status,end_date}]), `tables.spending_by_dept`, `departments` ([{department,revised_budget,ytd_expended,report_count}]), `resolutions` ([{resolution_number,title,status,amount,vendor,adopted_date}]), optional `errors`.

- [ ] **Step 1: Add the data-loading script to `templates/dashboard.html`**

After the existing tab-switch IIFE, add a second `<script>` that fetches and renders. Complete code:

```html
<script>
async function loadDashboard() {
  let d;
  try { const r = await fetch("/dashboard/data"); d = await r.json(); }
  catch (e) { return notice("Dashboard data unavailable."); }
  if (d.error) return notice(d.error);
  if (d.errors) notice("Some panels failed: " + Object.keys(d.errors).join(", "));
  renderKpis(d.kpis || {});
  renderGrants((d.tables && d.tables.grants) || []);
  renderDepartments(d.departments || []);
  renderResolutions(d.resolutions || []);
}
function notice(msg){ let n=document.getElementById("dash-notice"); if(n){ n.hidden=false; n.textContent=msg; } }
function money(v){ return v==null ? "—" : "$" + Number(v).toLocaleString(); }
function cell(v){ return v==null||v==="" ? "—" : v; }

function renderKpis(k){
  const cov=k.report_coverage||{filed:0,total_departments:0};
  const cards=[
    ["Active grants", (k.active_grants??0)+(k.grants_expiring_soon?` <span class="unit">(${k.grants_expiring_soon} exp.)</span>`:""), "info"],
    ["Grant funds active", money(k.grant_funds_active), "info"],
    ["YTD spend", money(k.ytd_spend)+' <span class="unit">/ '+money(k.revised_budget)+"</span>", "info"],
    ["Report coverage", `${cov.filed}<span class="unit">/${cov.total_departments}</span>`, "good"],
    ["Resolutions", k.resolutions_count??0, "info"],
  ];
  const el=document.getElementById("dash-kpis");
  if(el) el.innerHTML = cards.map(([l,v,c])=>
    `<div class="kpi ${c}"><div class="kpi-label">${l}</div><div class="kpi-value">${v}</div></div>`).join("");
}
function grantRow(g){
  return `<tr><td><div class="cell-strong">${cell(g.name)}</div></td><td>${cell(g.department)}</td>
    <td><span class="pill grey"><span class="tick"></span>${cell(g.status)}</span></td>
    <td class="num">${money(g.amount)}</td><td>—</td><td class="num">—</td>
    <td><span class="date-mono">${cell(g.end_date)}</span></td></tr>`;
}
function renderGrants(rows){
  const html = rows.length ? rows.map(grantRow).join("") :
    `<tr><td colspan="7"><div class="empty">No grants found.</div></td></tr>`;
  ["dash-grants-body","grants-body"].forEach(id=>{ const el=document.getElementById(id); if(el) el.innerHTML=html; });
}
function renderDepartments(rows){
  const el=document.getElementById("departments-cards"); if(!el) return;
  el.innerHTML = rows.length ? rows.map(r=>
    `<div class="dept-card"><div class="dc-head"><div class="dc-name">${cell(r.department)}</div></div>
      <div class="dc-stats">
        <div class="dc-stat"><div class="v">${r.report_count??0}</div><div class="l">Reports</div></div>
        <div class="dc-stat"><div class="v">${money(r.ytd_expended)}</div><div class="l">YTD spend</div></div>
        <div class="dc-stat"><div class="v">${money(r.revised_budget)}</div><div class="l">Budget</div></div>
      </div></div>`).join("") : `<div class="empty">No departments found.</div>`;
}
function renderResolutions(rows){
  const el=document.getElementById("resolutions-body"); if(!el) return;
  el.innerHTML = rows.length ? rows.map(r=>
    `<tr><td class="cell-strong">${cell(r.resolution_number)}</td><td>${cell(r.title)}</td>
      <td><span class="pill green"><span class="tick"></span>${cell(r.status)}</span></td>
      <td class="num">${money(r.amount)}</td><td>${cell(r.vendor)}</td>
      <td><span class="date-mono">${cell(r.adopted_date)}</span></td></tr>`).join("")
    : `<tr><td colspan="6"><div class="empty">No resolutions ingested yet.</div></td></tr>`;
}
loadDashboard();
</script>
```

Also add a notice element near the top of the main content (once): `<div id="dash-notice" class="empty" hidden></div>`. Ensure the Resolutions/Grants/Departments panels/tables from Task 2 have the exact ids referenced here (`dash-kpis`, `dash-grants-body`, `grants-body`, `departments-cards`, `resolutions-body`). Adjust the mockup's column order for grants if needed so `grantRow`'s 7 cells match the thead (Grant, Department, Status, Award, Match, Rollover, End date).

- [ ] **Step 2: Verify unit suite + route tests still green**

Run: `pytest -q -m "not integration"`
Expected: green. `python3 -c "import app"` clean.

- [ ] **Step 3: Operator visual verification (live env, not CI)**

Run the app against Supabase and open `/dashboard`:
Run: `python3 app.py` → open `http://localhost:5001/dashboard`
Expected: tabs switch; Dashboard KPIs show real numbers (active grants, grant funds, YTD/budget, report coverage, resolutions count); Grants tab lists real grants (match/rollover/spent = "—"); Departments tab shows real dept cards; **Resolutions tab lists the 7 real resolutions** (number, title, status, amount, vendor, date); Goals/Projects/Boards show empty-states; no console errors; no external chart-lib requests.

- [ ] **Step 4: Commit**

```bash
git add templates/dashboard.html
git commit -m "feat(dashboard): fetch + render real data into KPIs/grants/departments/resolutions"
```

---

## Self-Review

**Spec coverage:**
- §3 architecture (new dashboard.html shell, extended /dashboard/data, drop chart libs, Ask/Documents → main app) → Task 2 (shell) + Task 1 (aggregator) + Task 3 (bind). ✓
- §4 payload (departments, resolutions, grant_funds_active) → Task 1. ✓
- §2/§5 real-vs-empty per tab (Grants real w/ "—" match/rollover; Departments/Resolutions real; Goals/Projects/Boards empty-states) → Task 2 (empty-states) + Task 3 (real binding). ✓
- §9 Resolutions tab added → Task 2 (view + nav) + Task 1 (data) + Task 3 (render). ✓
- §7 testing (aggregator panels unit-tested; route/tabs test; operator visual) → Tasks 1–3. ✓
- Charts dropped → Task 2 test asserts no vis-timeline/chart.js. ✓

**Placeholder scan:** No TBD/TODO; Task 1 & 3 have complete code; Task 2 gives exact transformations against a concrete saved source file (not a placeholder). Empty-state copy is spelled out verbatim.

**Type consistency:** Task 3's JS reads exactly the keys Task 1 produces (`kpis.grant_funds_active`, `tables.grants[].{name,department,amount,status,end_date}`, `departments[].{department,revised_budget,ytd_expended,report_count}`, `resolutions[].{resolution_number,title,status,amount,vendor,adopted_date}`). The element ids referenced in Task 3 (`dash-kpis`, `dash-grants-body`, `grants-body`, `departments-cards`, `resolutions-body`, `dash-notice`) are exactly those Task 2 must create — this cross-task contract is stated in both tasks.

**Note:** `tables.grants` already exists in the current aggregator output (from `_build_tables`), so grants need no new aggregator method — Task 3 reads `d.tables.grants`. `spending_by_dept` likewise already exists if a spending panel is wanted later.
