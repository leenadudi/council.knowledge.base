# Timeline Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live in-app `/dashboard` page that visualizes the city's timeline information (grant lifecycles, report cadence, spending over time, resolutions when present) from Supabase.

**Architecture:** A `DashboardAggregator` runs read queries against the existing `SQLStore` and returns a JSON payload (KPIs, timeline, tables) with per-panel error isolation. Two Flask routes serve the page shell (`/dashboard`) and the data (`/dashboard/data`). A `dashboard.html` template fetches the data and renders it with vis-timeline (timeline lanes) and Chart.js (spending), both via CDN.

**Tech Stack:** Python 3.11+, Flask, psycopg2 (Supabase Postgres, RealDictCursor → dict rows), pytest; vis-timeline + Chart.js via CDN in the browser.

## Global Constraints

- Read-only: the dashboard MUST NOT write to any store.
- `/dashboard` and `/dashboard/data` return HTTP 503 when `app._ready` is False, matching existing routes (`/ask`, `/departments`).
- Per-panel error isolation: a failing panel query is caught, that panel is `null`/empty, and a message goes under `errors` — `build()` never raises.
- `SQLStore.cursor()` is a context manager yielding a **RealDictCursor** (rows are dicts: `row["col"]`). Use parameterized queries (`%s`), never string-interpolated values.
- Dates use timezone-aware `datetime.now(timezone.utc)` — never `datetime.utcnow()`.
- Quarter→marker-month mapping is exactly: Q1→1 (Jan), Q2→4 (Apr), Q3→7 (Jul), Q4→10 (Oct), day 1.
- "Active grant" = `status` in (active, in_progress, open, pending) case-insensitively, OR `end_date >= today`. "Expiring soon" = active AND `end_date` within 90 days of today.
- CDN libraries pinned to a specific version (vis-timeline, Chart.js). No build step.
- Tests run with `pytest`. Live-DB assertions are marked `@pytest.mark.integration` (the unit suite runs with `-m "not integration"`).

---

### Task 1: DashboardAggregator scaffold — KPIs + pure helpers

**Files:**
- Create: `src/dashboard/__init__.py`
- Create: `src/dashboard/aggregator.py`
- Test: `tests/dashboard/__init__.py`, `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Produces:
  - `quarter_start(year: int, quarter: str) -> datetime.date` (module function; Q1→Jan1 … Q4→Oct1; unknown quarter → month 1)
  - `class DashboardAggregator: __init__(self, sql_store, now: datetime | None = None)` — `now` injectable for deterministic tests; defaults to `datetime.now(timezone.utc)`.
  - `DashboardAggregator._build_kpis() -> dict` with keys `active_grants, grants_expiring_soon, ytd_spend, revised_budget, latest_period({year,quarter}|None), report_coverage({filed,total_departments}), resolutions_count, unclassified_docs`.
  - `DashboardAggregator._latest_period() -> dict | None` ({"year","quarter"} from MAX(year) then MAX(quarter) in `documents`, or None if empty).

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_aggregator.py
import datetime
from contextlib import contextmanager
from src.dashboard.aggregator import DashboardAggregator, quarter_start


class _FakeCursor:
    """Returns canned rows based on a substring match of the executed SQL."""
    def __init__(self, responses): self._responses = responses; self._last = None
    def execute(self, sql, params=None):
        self._last = next((v for k, v in self._responses.items() if k in sql), [])
    def fetchall(self): return list(self._last)
    def fetchone(self): return self._last[0] if self._last else None


class _FakeStore:
    def __init__(self, responses): self._responses = responses
    @contextmanager
    def cursor(self):
        yield _FakeCursor(self._responses)


def test_quarter_start_mapping():
    assert quarter_start(2026, "Q1") == datetime.date(2026, 1, 1)
    assert quarter_start(2026, "Q3") == datetime.date(2026, 7, 1)
    assert quarter_start(2026, "Q9") == datetime.date(2026, 1, 1)  # unknown → Jan


def test_kpis_shape_and_coverage():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "FROM grants WHERE": [{"active": 12, "expiring": 3}],
        "FROM expenditures": [{"ytd": 4200000.0, "budget": 9000000.0}],
        "MAX(year)": [{"year": 2026}],
        "MAX(quarter)": [{"quarter": "Q1"}],
        "coverage_filed": [{"filed": 8}],
        "coverage_total": [{"total": 14}],
        "FROM resolutions": [{"c": 0}],
        "document_type='unclassified'": [{"c": 1}],
    })
    kpis = DashboardAggregator(store, now=now)._build_kpis()
    assert kpis["active_grants"] == 12
    assert kpis["grants_expiring_soon"] == 3
    assert kpis["latest_period"] == {"year": 2026, "quarter": "Q1"}
    assert kpis["report_coverage"] == {"filed": 8, "total_departments": 14}
    assert kpis["resolutions_count"] == 0
    assert kpis["unclassified_docs"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/dashboard/test_aggregator.py -v`
Expected: FAIL with `ModuleNotFoundError: src.dashboard.aggregator`

- [ ] **Step 3: Implement the scaffold + KPIs**

```python
# src/dashboard/__init__.py
"""Dashboard data aggregation."""
```

```python
# src/dashboard/aggregator.py
"""Read-only aggregation of timeline data for the /dashboard page."""
from __future__ import annotations

import datetime
import logging
from datetime import timezone

logger = logging.getLogger(__name__)

_Q_MONTH = {"Q1": 1, "Q2": 4, "Q3": 7, "Q4": 10}
_ACTIVE_STATUSES = ("active", "in_progress", "open", "pending")


def quarter_start(year: int, quarter: str) -> datetime.date:
    return datetime.date(year, _Q_MONTH.get((quarter or "").upper(), 1), 1)


class DashboardAggregator:
    def __init__(self, sql_store, now: datetime.datetime | None = None):
        self.sql = sql_store
        self.now = now or datetime.datetime.now(timezone.utc)

    # -- KPIs -------------------------------------------------------------
    def _latest_period(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT MAX(year) AS year FROM documents")
            row = cur.fetchone()
            year = row and row.get("year")
            if not year:
                return None
            cur.execute("SELECT MAX(quarter) AS quarter FROM documents WHERE year = %s", (year,))
            q = cur.fetchone()
            return {"year": int(year), "quarter": (q and q.get("quarter")) or ""}

    def _build_kpis(self) -> dict:
        today = self.now.date()
        soon = today + datetime.timedelta(days=90)
        statuses = list(_ACTIVE_STATUSES)
        with self.sql.cursor() as cur:
            cur.execute(
                """SELECT
                     COUNT(*) FILTER (WHERE LOWER(status) = ANY(%s) OR end_date >= %s) AS active,
                     COUNT(*) FILTER (WHERE (LOWER(status) = ANY(%s) OR end_date >= %s)
                                       AND end_date IS NOT NULL AND end_date <= %s) AS expiring
                   FROM grants WHERE TRUE""",
                (statuses, today, statuses, today, soon),
            )
            g = cur.fetchone() or {}
            cur.execute(
                "SELECT COALESCE(SUM(ytd_expended),0) AS ytd, COALESCE(SUM(revised_budget),0) AS budget FROM expenditures",
            )
            e = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS c FROM resolutions")
            res = cur.fetchone() or {}
            cur.execute("SELECT COUNT(*) AS c FROM documents WHERE document_type='unclassified'")
            unc = cur.fetchone() or {}

        latest = self._latest_period()
        coverage = {"filed": 0, "total_departments": 0}
        if latest:
            with self.sql.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(DISTINCT department) AS filed FROM documents "
                    "WHERE document_type='quarterly_report' AND year=%s AND quarter=%s  -- coverage_filed",
                    (latest["year"], latest["quarter"]),
                )
                f = cur.fetchone() or {}
                cur.execute("SELECT COUNT(DISTINCT department) AS total FROM documents  -- coverage_total")
                t = cur.fetchone() or {}
            coverage = {"filed": int(f.get("filed", 0) or 0), "total_departments": int(t.get("total", 0) or 0)}

        return {
            "active_grants": int(g.get("active", 0) or 0),
            "grants_expiring_soon": int(g.get("expiring", 0) or 0),
            "ytd_spend": float(e.get("ytd", 0) or 0),
            "revised_budget": float(e.get("budget", 0) or 0),
            "latest_period": latest,
            "report_coverage": coverage,
            "resolutions_count": int(res.get("c", 0) or 0),
            "unclassified_docs": int(unc.get("c", 0) or 0),
        }
```

(The `-- coverage_filed` / `-- coverage_total` SQL comments give the test's substring matcher distinct keys; they are inert in real SQL.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/dashboard/test_aggregator.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/ tests/dashboard/
git commit -m "feat(dashboard): aggregator scaffold with KPI panel"
```

---

### Task 2: Timeline panel

**Files:**
- Modify: `src/dashboard/aggregator.py`
- Test: `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Consumes: `quarter_start`, `DashboardAggregator` (Task 1).
- Produces: `DashboardAggregator._build_timeline() -> dict` with keys `grants` (range items: id,label,department,start,end,status,amount), `reports` (point items: id,department,date,quarter,year,document_type), `resolutions` (point items: id,label,date,amount,status), `spending` (period:"YYYY Qn", ytd_expended). Dates serialized as ISO strings.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/dashboard/test_aggregator.py
def test_timeline_shapes_dates_and_handles_empty_resolutions():
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "NEHA-FDA", "department": "Health Office",
             "start_date": datetime.date(2025, 1, 1), "end_date": datetime.date(2026, 1, 1),
             "status": "active", "amount": 14000.0},
        ],
        "FROM documents": [
            {"id": 10, "department": "Codes", "quarter": "Q2", "year": 2025, "document_type": "quarterly_report"},
        ],
        "FROM resolutions": [],  # empty today
        "GROUP BY year, quarter": [
            {"year": 2025, "quarter": "Q1", "ytd": 100000.0},
            {"year": 2025, "quarter": "Q2", "ytd": 150000.0},
        ],
    })
    tl = DashboardAggregator(store)._build_timeline()
    assert tl["grants"][0]["start"] == "2025-01-01" and tl["grants"][0]["end"] == "2026-01-01"
    assert tl["reports"][0]["date"] == "2025-04-01"  # Q2 → Apr 1
    assert tl["resolutions"] == []
    assert tl["spending"][1]["period"] == "2025 Q2" and tl["spending"][1]["ytd_expended"] == 150000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/dashboard/test_aggregator.py::test_timeline_shapes_dates_and_handles_empty_resolutions -v`
Expected: FAIL with `AttributeError: ... _build_timeline`

- [ ] **Step 3: Implement `_build_timeline`**

```python
    # add inside DashboardAggregator
    def _build_timeline(self) -> dict:
        def iso(d):
            return d.isoformat() if hasattr(d, "isoformat") else (str(d) if d else None)

        with self.sql.cursor() as cur:
            cur.execute(
                "SELECT id, grant_name, department, start_date, end_date, status, amount "
                "FROM grants WHERE start_date IS NOT NULL ORDER BY start_date"
            )
            grants = []
            for r in cur.fetchall():
                start = r["start_date"]
                end = r["end_date"] or (start.replace(year=start.year + 1) if start else None)
                grants.append({
                    "id": f"grant-{r['id']}", "label": r.get("grant_name") or "Grant",
                    "department": r.get("department"), "start": iso(start), "end": iso(end),
                    "status": r.get("status"), "amount": float(r["amount"]) if r.get("amount") is not None else None,
                })

            cur.execute(
                "SELECT id, department, quarter, year, document_type FROM documents "
                "WHERE year IS NOT NULL AND quarter <> '' ORDER BY year, quarter"
            )
            reports = []
            for r in cur.fetchall():
                reports.append({
                    "id": f"report-{r['id']}", "department": r.get("department"),
                    "date": quarter_start(int(r["year"]), r.get("quarter")).isoformat(),
                    "quarter": r.get("quarter"), "year": int(r["year"]),
                    "document_type": r.get("document_type"),
                })

            cur.execute(
                "SELECT id, resolution_number, title, adopted_date, amount, status "
                "FROM resolutions WHERE adopted_date IS NOT NULL ORDER BY adopted_date"
            )
            resolutions = []
            for r in cur.fetchall():
                resolutions.append({
                    "id": f"res-{r['id']}", "label": r.get("resolution_number") or r.get("title") or "Resolution",
                    "date": iso(r["adopted_date"]), "amount": float(r["amount"]) if r.get("amount") is not None else None,
                    "status": r.get("status"),
                })

            cur.execute(
                "SELECT year, quarter, COALESCE(SUM(ytd_expended),0) AS ytd FROM expenditures "
                "WHERE year IS NOT NULL GROUP BY year, quarter ORDER BY year, quarter"
            )
            spending = [{"period": f"{r['year']} {r['quarter']}", "ytd_expended": float(r["ytd"] or 0)}
                        for r in cur.fetchall()]

        return {"grants": grants, "reports": reports, "resolutions": resolutions, "spending": spending}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/dashboard/test_aggregator.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): timeline panel (grants/reports/resolutions/spending)"
```

---

### Task 3: Tables panel + build() assembly with error isolation

**Files:**
- Modify: `src/dashboard/aggregator.py`
- Test: `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Consumes: Task 1 + 2 methods.
- Produces:
  - `DashboardAggregator._build_tables() -> dict` with keys `grants`, `spending_by_dept`, `reports` (lists of dict rows).
  - `DashboardAggregator.build() -> dict` — top-level keys `generated_at` (ISO), `kpis`, `timeline`, `tables`, and `errors` (present only if a panel failed). A panel that raises is caught, its value set to `None`, and the message recorded under `errors`; `build()` never raises.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/dashboard/test_aggregator.py
def test_build_assembles_payload_and_isolates_panel_errors():
    store = _FakeStore({
        "FROM grants WHERE": [{"active": 0, "expiring": 0}],
        "FROM expenditures": [{"ytd": 0, "budget": 0}],
        "MAX(year)": [{"year": None}],
        "FROM resolutions": [],
        "document_type='unclassified'": [{"c": 0}],
        "GROUP BY department": [{"department": "Codes", "revised_budget": 1.0, "ytd_expended": 0.5}],
        "FROM documents ORDER BY": [{"department": "Codes", "quarter": "Q1", "year": 2026, "document_type": "quarterly_report"}],
    })
    out = DashboardAggregator(store).build()
    assert set(out) >= {"generated_at", "kpis", "timeline", "tables"}
    assert out["tables"]["spending_by_dept"][0]["department"] == "Codes"


class _BoomStore:
    from contextlib import contextmanager as _cm
    @_cm
    def cursor(self):
        raise RuntimeError("db down")
        yield  # pragma: no cover


def test_build_never_raises_records_errors():
    out = DashboardAggregator(_BoomStore()).build()
    assert out["kpis"] is None and out["timeline"] is None and out["tables"] is None
    assert "kpis" in out["errors"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/dashboard/test_aggregator.py -k "build" -v`
Expected: FAIL with `AttributeError: ... build` / `_build_tables`

- [ ] **Step 3: Implement tables + build()**

```python
    # add inside DashboardAggregator
    def _build_tables(self) -> dict:
        with self.sql.cursor() as cur:
            cur.execute(
                "SELECT grant_name, department, amount, start_date, end_date, status "
                "FROM grants ORDER BY start_date DESC NULLS LAST LIMIT 200"
            )
            grants = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT department, COALESCE(SUM(revised_budget),0) AS revised_budget, "
                "COALESCE(SUM(ytd_expended),0) AS ytd_expended FROM expenditures "
                "GROUP BY department ORDER BY ytd_expended DESC"
            )
            spending_by_dept = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT department, quarter, year, document_type FROM documents "
                "ORDER BY year DESC, quarter DESC LIMIT 200"
            )
            reports = [dict(r) for r in cur.fetchall()]
        # JSON-safe: coerce date/Decimal to str/float
        def clean(rows):
            for row in rows:
                for k, v in list(row.items()):
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat()
                    elif isinstance(v, (int, float)) or v is None or isinstance(v, str):
                        pass
                    else:
                        row[k] = float(v)  # Decimal
            return rows
        return {"grants": clean(grants), "spending_by_dept": clean(spending_by_dept), "reports": clean(reports)}

    def _safe(self, name, fn, errors):
        try:
            return fn()
        except Exception as e:
            logger.warning("dashboard panel %s failed: %s", name, e)
            errors[name] = str(e)
            return None

    def build(self) -> dict:
        errors: dict = {}
        out = {
            "generated_at": self.now.isoformat(),
            "kpis": self._safe("kpis", self._build_kpis, errors),
            "timeline": self._safe("timeline", self._build_timeline, errors),
            "tables": self._safe("tables", self._build_tables, errors),
        }
        if errors:
            out["errors"] = errors
        return out
```

(Add `# GROUP BY department` and `# FROM documents ORDER BY` are already distinct substrings present in the SQL above for the fake matcher — verify the test keys match real SQL substrings.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/dashboard/test_aggregator.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): tables panel + build() with per-panel error isolation"
```

---

### Task 4: Flask routes + nav link

**Files:**
- Modify: `app.py` (add two routes after the existing routes)
- Modify: `templates/index.html` (add a nav link to `/dashboard`)
- Test: `tests/dashboard/test_dashboard_route.py`

**Interfaces:**
- Consumes: `DashboardAggregator(_sql_store).build()` (Tasks 1–3); module globals `_ready`, `_sql_store` in `app.py`.
- Produces: `GET /dashboard` (200 HTML, or 503 when not ready) and `GET /dashboard/data` (200 JSON of `build()`, or 503 when not ready).

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_dashboard_route.py
import app as appmod


def _client():
    return appmod.app.test_client()


def test_dashboard_data_ready(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", True)
    monkeypatch.setattr(appmod, "_sql_store", object())
    class _Agg:
        def __init__(self, store): pass
        def build(self): return {"generated_at": "t", "kpis": {}, "timeline": {}, "tables": {}}
    monkeypatch.setattr(appmod, "DashboardAggregator", _Agg, raising=False)
    r = _client().get("/dashboard/data")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) >= {"generated_at", "kpis", "timeline", "tables"}


def test_dashboard_data_not_ready(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", False)
    r = _client().get("/dashboard/data")
    assert r.status_code == 503


def test_dashboard_page_renders(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", True)
    r = _client().get("/dashboard")
    assert r.status_code == 200
    assert b"dashboard" in r.data.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/dashboard/test_dashboard_route.py -v`
Expected: FAIL (routes/`DashboardAggregator` symbol not defined; 404)

- [ ] **Step 3: Implement the routes**

Add the import near the top of `app.py` with the other imports:

```python
from src.dashboard.aggregator import DashboardAggregator
```

Add after the existing routes (e.g. after `/departments/<...>/staff`):

```python
@app.route("/dashboard")
def dashboard():
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    return render_template("dashboard.html")


@app.route("/dashboard/data")
def dashboard_data():
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    try:
        return jsonify(DashboardAggregator(_sql_store).build())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

Add a nav link in `templates/index.html` (near the top header/nav area — match existing markup):

```html
<a href="/dashboard" class="nav-link">📊 Dashboard</a>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/dashboard/test_dashboard_route.py -v`
Expected: PASS (3 tests). Note: `test_dashboard_page_renders` requires `templates/dashboard.html` to exist — if Task 5 is not yet done, create a minimal `<h1>Dashboard</h1>` placeholder so this passes, then flesh it out in Task 5.

- [ ] **Step 5: Commit**

```bash
git add app.py templates/index.html tests/dashboard/test_dashboard_route.py
git commit -m "feat(dashboard): /dashboard and /dashboard/data routes + nav link"
```

---

### Task 5: dashboard.html template (KPI strip + vis-timeline + Chart.js)

**Files:**
- Create/replace: `templates/dashboard.html` (replacing any placeholder from Task 4)

**Interfaces:**
- Consumes: `GET /dashboard/data` payload (Tasks 1–3 shape).

- [ ] **Step 1: Write `templates/dashboard.html`**

A self-contained page that, on load, `fetch("/dashboard/data")` and renders:
- KPI strip — cards for active_grants (+ expiring), ytd_spend vs revised_budget, report_coverage (filed/total), resolutions_count, unclassified_docs.
- Timeline — a `vis.Timeline` with three groups (Grants=ranges using start/end, Reports=points, Resolutions=points). Empty lanes show "No <thing> yet".
- Spending — a Chart.js bar chart over `timeline.spending`.
- Detail tables — grants, spending_by_dept, reports.
- If the fetch fails or `errors` is present, show an inline notice; never blank.

CDN tags (pinned), in `<head>`:

```html
<link href="https://cdnjs.cloudflare.com/ajax/libs/vis-timeline/7.7.3/vis-timeline-graph2d.min.css" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-timeline/7.7.3/vis-timeline-graph2d.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
```

Body skeleton + JS (complete enough to render; style to match `index.html` colors/fonts):

```html
<div class="dash">
  <h1>📊 City Timeline Dashboard</h1>
  <div id="kpis" class="kpi-strip"></div>
  <h2>Timeline</h2>
  <div id="timeline"></div>
  <h2>Spending by period</h2>
  <canvas id="spendChart" height="120"></canvas>
  <div id="tables"></div>
  <div id="notice" class="notice" hidden></div>
</div>
<script>
async function load() {
  let data;
  try { const r = await fetch("/dashboard/data"); data = await r.json(); }
  catch (e) { return showNotice("Dashboard data unavailable."); }
  if (data.error) return showNotice(data.error);
  if (data.errors) showNotice("Some panels failed: " + Object.keys(data.errors).join(", "));
  renderKpis(data.kpis); renderTimeline(data.timeline); renderSpend(data.timeline);
  renderTables(data.tables);
}
function showNotice(msg){ const n=document.getElementById("notice"); n.hidden=false; n.textContent=msg; }
function fmtMoney(v){ return v==null?"—":"$"+Number(v).toLocaleString(); }
function renderKpis(k){
  if(!k) return;
  const cov=k.report_coverage||{filed:0,total_departments:0};
  document.getElementById("kpis").innerHTML = [
    ["Active grants", k.active_grants + (k.grants_expiring_soon?` (${k.grants_expiring_soon} expiring)`:"")],
    ["YTD spend", fmtMoney(k.ytd_spend)+" / "+fmtMoney(k.revised_budget)],
    ["Report coverage", `${cov.filed}/${cov.total_departments}`],
    ["Resolutions", k.resolutions_count],
    ["Needs review", k.unclassified_docs],
  ].map(([t,v])=>`<div class="kpi"><div class="kpi-v">${v}</div><div class="kpi-t">${t}</div></div>`).join("");
}
function renderTimeline(tl){
  if(!tl) return;
  const groups = new vis.DataSet([
    {id:"grants",content:"Grants"},{id:"reports",content:"Reports"},{id:"resolutions",content:"Resolutions"}]);
  const items = new vis.DataSet();
  (tl.grants||[]).forEach(g=>items.add({group:"grants",id:g.id,content:g.label,start:g.start,end:g.end,title:`${g.department||""} ${fmtMoney(g.amount)} (${g.status||""})`}));
  (tl.reports||[]).forEach(r=>items.add({group:"reports",id:r.id,content:r.department||"",start:r.date,type:"point",title:`${r.quarter} ${r.year}`}));
  (tl.resolutions||[]).forEach(x=>items.add({group:"resolutions",id:x.id,content:x.label,start:x.date,type:"point"}));
  new vis.Timeline(document.getElementById("timeline"), items, groups, {stack:true, zoomable:true});
  if(!(tl.resolutions||[]).length){ /* empty lane shows label only */ }
}
function renderSpend(tl){
  const s=(tl&&tl.spending)||[];
  new Chart(document.getElementById("spendChart"), {type:"bar",
    data:{labels:s.map(x=>x.period),datasets:[{label:"YTD expended",data:s.map(x=>x.ytd_expended)}]},
    options:{plugins:{legend:{display:false}}}});
}
function renderTables(t){
  if(!t) return;
  const tbl=(rows,cols)=>!rows||!rows.length?"<p class='muted'>No data yet</p>":
    `<table><thead><tr>${cols.map(c=>`<th>${c}</th>`).join("")}</tr></thead><tbody>`+
    rows.map(r=>`<tr>${cols.map(c=>`<td>${r[c]??""}</td>`).join("")}</tr>`).join("")+"</tbody></table>";
  document.getElementById("tables").innerHTML =
    "<h2>Grants</h2>"+tbl(t.grants,["grant_name","department","amount","start_date","end_date","status"])+
    "<h2>Spending by department</h2>"+tbl(t.spending_by_dept,["department","revised_budget","ytd_expended"])+
    "<h2>Recent reports</h2>"+tbl(t.reports,["department","quarter","year","document_type"]);
}
load();
</script>
```

(Include a `<style>` block matching `index.html`'s palette: `.kpi-strip{display:flex;gap:1rem;flex-wrap:wrap}` etc. Keep it readable.)

- [ ] **Step 2: Verify the page renders (manual / integration)**

Run the app against the live DB and open `/dashboard`:
Run: `python3 -m flask --app app run` (or the project's run command in `run.sh`)
Expected: KPI cards populate; timeline shows grant bars + report markers; Resolutions lane shows "No resolutions yet"; spending bar chart renders; tables populate. (This requires Supabase reachable + the CDN reachable from the browser — it is not a unit test.)

- [ ] **Step 3: Confirm the unit suite is still green**

Run: `pytest -q -m "not integration"`
Expected: all green (route + aggregator tests pass; `test_dashboard_page_renders` now hits the real template).

- [ ] **Step 4: Commit**

```bash
git add templates/dashboard.html
git commit -m "feat(dashboard): timeline dashboard template (vis-timeline + Chart.js)"
```

---

## Self-Review

**Spec coverage:**
- §2 architecture (aggregator / 2 routes / template / nav) → Tasks 1-3 (aggregator), 4 (routes+nav), 5 (template). ✓
- §3 payload + every KPI/timeline/table field + exact queries → Tasks 1-3 (shapes and SQL match the spec's keys; quarter→month mapping in `quarter_start`; active/expiring logic in `_build_kpis`; unclassified_docs from `documents`). ✓
- §4 vis-timeline ranges+points+lanes, Chart.js spending, empty states, CDN pinned → Task 5. ✓
- §5 error handling: per-panel isolation (`_safe`/`build`), 503 not-ready, page-level fetch-failure notice → Tasks 3 (build) + 4 (routes) + 5 (showNotice). ✓
- §6 testing: aggregator unit (fake cursor), route (stubbed aggregator), integration marked, rendering manual → Tasks 1-4 tests + Task 5 manual. ✓

**Placeholder scan:** No TBD/TODO; every code step has real code. Task 4 notes a minimal placeholder template so its route test passes before Task 5 — acceptable and explicit. CDN versions are pinned (7.7.3 / 4.4.1).

**Type consistency:** `DashboardAggregator(sql_store, now=None)` and `build()`/`_build_kpis`/`_build_timeline`/`_build_tables`/`_safe` names are consistent across tasks. Payload keys used in the template (Task 5) match those produced in Tasks 1-3 (`kpis.active_grants`, `timeline.grants[].start/end`, `timeline.spending[].period/ytd_expended`, `tables.spending_by_dept[].department`, etc.). The fake-cursor substring keys in tests correspond to real substrings in the SQL.

**Note on test infra:** create `tests/dashboard/__init__.py` (Task 1). The route test imports `app` — `_init()` runs at import and may log a startup error if the DB is unreachable, but the tests monkeypatch `_ready`/`_sql_store`/`DashboardAggregator`, so they do not require a live DB.
