# Projects (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live-derived Projects layer + tab that unifies grant-funded initiatives and resolution-authorized work (land development, contracts, grant actions) into one "how are projects doing" view.

**Architecture:** A new `src/dashboard/projects.py` `Projects` builder aggregates the `grants` and `resolutions` tables at build time — classifying each resolution by type, normalizing statuses to a common lifecycle, and bucketing administrative items — then it is wired into `DashboardAggregator.build()` as a `projects` panel via `_safe()`. The frontend adds a Projects nav item, view, and `renderProjects()` that reads `D.projects`, with a detail slide-over reusing the existing `#dossier` sheet. Everything is deterministic — no LLM, no new tables, no ingestion; the list updates automatically as more resolutions are ingested.

**Tech Stack:** Python 3, Flask, psycopg2 (RealDictCursor), Jinja2, vanilla JS in `templates/redesign.html`, pytest.

## Global Constraints

- **Zero LLM cost.** Projects is pure deterministic SQL aggregation; it must run inside the always-on `DashboardAggregator.build()` (served by the 90s-cached `/dashboard/data`) and issue no model calls.
- **Live-derived, no new table.** Assemble from `grants` + `resolutions` every build; no migration, no materialized `projects` table.
- **Follow existing patterns:** reuse `DashboardAggregator._dept_key` / `_dept_display`; mirror `ReviewQuestions` module style; tests use the `_FakeStore`/`_FakeCursor` substring-match mocks; frontend reuses `esc`/`cell`/`money`/`dkey`/`deptDisplay` helpers, `.filter`/`.pill`/`.card` CSS, and the `#dossier` / `#dossier-overlay` slide-over with `closeDossier`.
- **Canonical department handling stays in the frontend filter** (via `dkey`/`deptDisplay`), matching Goals/Finances; the backend passes the department string through as stored.
- Spec: `docs/superpowers/specs/2026-07-09-projects-phase1-design.md`.

---

### Task 1: `Projects` data layer (`src/dashboard/projects.py`)

**Files:**
- Create: `src/dashboard/projects.py`
- Test: `tests/dashboard/test_projects.py`

**Interfaces:**
- Consumes: a `sql_store` with a `.cursor()` context manager (same object `DashboardAggregator` uses); `DashboardAggregator._dept_key` / `_dept_display`.
- Produces:
  - `classify_resolution(title: str) -> str` → one of `land_development|grant_action|contract|budget|appointment|other`
  - `normalize_grant_status(s: str) -> str` and `normalize_resolution_status(s: str) -> str` → one of `Proposed|Active|Awarded|Completed|Closed|Stalled`
  - `Projects(sql_store, now=None).build() -> dict` with keys `projects`, `administrative`, `counts` (`{active, attention, by_type}`), `funding_in_flight`. Each project record: `{id, source, type, title, department, party, amount, status, date, end_date, source_file, resolution_number, attention}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/dashboard/test_projects.py`:

```python
# tests/dashboard/test_projects.py
import datetime
from contextlib import contextmanager

from src.dashboard.projects import (
    Projects, classify_resolution,
    normalize_grant_status, normalize_resolution_status,
)


class _FakeCursor:
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


def test_classify_resolution_by_title():
    assert classify_resolution("A Resolution approving the Preliminary/Final Land Development") == "land_development"
    assert classify_resolution("A Resolution authorizing the submission of a grant application") == "grant_action"
    assert classify_resolution("A Resolution authorizing a professional services agreement") == "contract"
    assert classify_resolution("A Resolution approving the First Proposed 2026 Budget") == "budget"
    assert classify_resolution("A Resolution reappointing a board member") == "appointment"
    assert classify_resolution("A Resolution honoring the retiring chief") == "other"
    assert classify_resolution("") == "other"


def test_normalize_statuses():
    assert normalize_grant_status("active") == "Active"
    assert normalize_grant_status("AWARDED") == "Awarded"
    assert normalize_grant_status("applied") == "Proposed"
    assert normalize_grant_status("pending") == "Proposed"
    assert normalize_grant_status("closed") == "Closed"
    assert normalize_grant_status("weird") == "Active"          # default
    assert normalize_resolution_status("Passed") == "Active"
    assert normalize_resolution_status("Tabled") == "Stalled"
    assert normalize_resolution_status("Failed") == "Closed"
    assert normalize_resolution_status("") == "Proposed"        # default


def test_build_assembles_typed_projects_and_buckets_administrative():
    now = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "TASA Grant (Walnut St.)", "department": "Public Works",
             "amount": 1000000.0, "start_date": datetime.date(2025, 1, 1),
             "end_date": datetime.date(2026, 5, 1), "status": "awarded", "source_file": "g.pdf"},
            {"id": 2, "grant_name": "", "department": "X", "amount": None,
             "start_date": None, "end_date": None, "status": "active", "source_file": None},  # blank title -> skipped
        ],
        "FROM resolutions": [
            {"resolution_number": "5-2026", "title": "A Resolution approving a Land Development plan",
             "vendor": "Pennmark", "amount": None, "department": "Bureau of Planning",
             "adopted_date": datetime.date(2026, 1, 15), "status": "Passed", "source_file": "r5.pdf"},
            {"resolution_number": "3-2026", "title": "A Resolution approving the 2026 Budget",
             "vendor": "", "amount": None, "department": "Finance",
             "adopted_date": datetime.date(2026, 1, 15), "status": "Passed", "source_file": "r3.pdf"},
        ],
    })
    out = Projects(store, now=now).build()
    assert set(out) == {"projects", "administrative", "counts", "funding_in_flight"}
    # blank-title grant dropped; land-development resolution kept; budget bucketed
    titles = [p["title"] for p in out["projects"]]
    assert "TASA Grant (Walnut St.)" in titles
    assert any(p["type"] == "land_development" for p in out["projects"])
    assert len(out["administrative"]) == 1 and out["administrative"][0]["type"] == "budget"
    # types + status normalization surfaced
    grant = next(p for p in out["projects"] if p["source"] == "grant")
    assert grant["type"] == "grant" and grant["status"] == "Awarded"
    assert grant["id"] == "grant-1" and grant["end_date"] == "2026-05-01"
    res = next(p for p in out["projects"] if p["source"] == "resolution")
    assert res["id"] == "res-5-2026" and res["party"] == "Pennmark" and res["status"] == "Active"
    # funding_in_flight sums active/awarded/proposed project amounts
    assert out["funding_in_flight"] == 1000000.0
    assert out["counts"]["by_type"]["grant"] == 1


def test_build_flags_expiring_grant_as_attention():
    now = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)  # today = 2026-03-01
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "Expiring soon", "department": "PW", "amount": 5.0,
             "start_date": None, "end_date": datetime.date(2026, 4, 1), "status": "active", "source_file": None},
            {"id": 2, "grant_name": "Far off", "department": "PW", "amount": 5.0,
             "start_date": None, "end_date": datetime.date(2027, 1, 1), "status": "active", "source_file": None},
        ],
        "FROM resolutions": [],
    })
    out = Projects(store, now=now).build()
    a = {p["title"]: p["attention"] for p in out["projects"]}
    assert a["Expiring soon"] is True and a["Far off"] is False
    assert out["counts"]["attention"] == 1


def test_build_empty_tables():
    store = _FakeStore({"FROM grants": [], "FROM resolutions": []})
    out = Projects(store).build()
    assert out == {"projects": [], "administrative": [],
                   "counts": {"active": 0, "attention": 0, "by_type": {}},
                   "funding_in_flight": 0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/dashboard/test_projects.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.dashboard.projects'`.

- [ ] **Step 3: Write the implementation**

Create `src/dashboard/projects.py`:

```python
"""Live-derived Projects layer over grants + resolutions (deterministic, no LLM).

A "project" is a synthesis over already-extracted data, NOT a new table: every
grant is a grant-funded project, and each resolution is classified by type and
surfaced as an initiative (land development, contract, grant action) or bucketed
as administrative (budget/appointment). Assembled fresh every build, so newly
ingested resolutions appear automatically. See
docs/superpowers/specs/2026-07-09-projects-phase1-design.md.
"""
from __future__ import annotations

import datetime
import logging
import re
from collections import Counter
from datetime import timezone

from src.dashboard.aggregator import DashboardAggregator

logger = logging.getLogger(__name__)

_EXPIRING_DAYS = 120

# Resolution title -> type. First match wins; order matters (land dev before grant).
_TYPE_RULES = [
    ("land_development", re.compile(r"land development|subdivision|zoning|plat|rezon", re.I)),
    ("grant_action",     re.compile(r"grant", re.I)),
    ("contract",         re.compile(r"agreement|contract|professional services|purchase|negotiat|lease", re.I)),
    ("budget",           re.compile(r"budget|appropriat|millage|\btax\b", re.I)),
    ("appointment",      re.compile(r"appoint|reappoint|resign|confirm", re.I)),
]
_ADMIN_TYPES = {"budget", "appointment"}

_GRANT_STATUS = {"active": "Active", "awarded": "Awarded", "applied": "Proposed",
                 "pending": "Proposed", "closed": "Closed"}
_RES_STATUS_RULES = [
    (re.compile(r"tabl", re.I), "Stalled"),
    (re.compile(r"fail|defeat|reject", re.I), "Closed"),
    (re.compile(r"pass|adopt|approv|ratif", re.I), "Active"),
]


def classify_resolution(title: str) -> str:
    t = title or ""
    for name, rx in _TYPE_RULES:
        if rx.search(t):
            return name
    return "other"


def normalize_grant_status(s: str) -> str:
    return _GRANT_STATUS.get((s or "").strip().lower(), "Active")


def normalize_resolution_status(s: str) -> str:
    for rx, val in _RES_STATUS_RULES:
        if rx.search(s or ""):
            return val
    return "Proposed"


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else d


class Projects:
    """Assembles the live Projects layer from grants + resolutions."""

    def __init__(self, sql_store, now: datetime.datetime | None = None):
        self.sql = sql_store
        self.now = now or datetime.datetime.now(timezone.utc)

    def build(self) -> dict:
        today = self.now.date()
        horizon = today + datetime.timedelta(days=_EXPIRING_DAYS)
        with self.sql.cursor() as cur:
            cur.execute("SELECT id, grant_name, department, amount, start_date, "
                        "end_date, status, source_file FROM grants")
            grant_rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT resolution_number, title, vendor, amount, department, "
                        "adopted_date, status, source_file FROM resolutions")
            res_rows = [dict(r) for r in cur.fetchall()]

        projects: list[dict] = []
        administrative: list[dict] = []

        for r in grant_rows:
            title = (r.get("grant_name") or "").strip()
            if not title:
                continue
            amount = float(r["amount"]) if r.get("amount") is not None else None
            status = normalize_grant_status(r.get("status"))
            end = r.get("end_date")
            expiring = bool(end and hasattr(end, "toordinal") and today <= end <= horizon)
            projects.append({
                "id": f"grant-{r['id']}", "source": "grant", "type": "grant",
                "title": title, "department": r.get("department"),
                "party": None, "amount": amount, "status": status,
                "date": _iso(r.get("start_date")), "end_date": _iso(end),
                "source_file": r.get("source_file"), "resolution_number": None,
                "attention": expiring or status == "Stalled",
            })

        for r in res_rows:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            typ = classify_resolution(title)
            amount = float(r["amount"]) if r.get("amount") is not None else None
            status = normalize_resolution_status(r.get("status"))
            rec = {
                "id": f"res-{r.get('resolution_number')}", "source": "resolution", "type": typ,
                "title": title, "department": r.get("department"),
                "party": (r.get("vendor") or None), "amount": amount, "status": status,
                "date": _iso(r.get("adopted_date")), "end_date": None,
                "source_file": r.get("source_file"), "resolution_number": r.get("resolution_number"),
                "attention": status == "Stalled",
            }
            (administrative if typ in _ADMIN_TYPES else projects).append(rec)

        # attention first, then largest funding, then title
        projects.sort(key=lambda p: (0 if p["attention"] else 1, -(p["amount"] or 0), p["title"].lower()))
        administrative.sort(key=lambda p: p["title"].lower())

        counts = {
            "active": sum(1 for p in projects if p["status"] == "Active"),
            "attention": sum(1 for p in projects if p["attention"]),
            "by_type": dict(Counter(p["type"] for p in projects)),
        }
        funding = sum(p["amount"] or 0 for p in projects if p["status"] in ("Active", "Awarded", "Proposed"))
        return {"projects": projects, "administrative": administrative,
                "counts": counts, "funding_in_flight": funding}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/dashboard/test_projects.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/projects.py tests/dashboard/test_projects.py
git commit -m "feat(dashboard): Projects data layer over grants + resolutions"
```

---

### Task 2: Wire `projects` into `DashboardAggregator.build()`

**Files:**
- Modify: `src/dashboard/aggregator.py` (add `_build_projects`; register panel in `build()`)
- Test: `tests/dashboard/test_aggregator.py` (extend shape test + error-set test)

**Interfaces:**
- Consumes: `Projects(self.sql).build()` from Task 1.
- Produces: `DashboardAggregator.build()` output gains a `projects` key with sub-keys `{projects, administrative, counts, funding_in_flight}`.

- [ ] **Step 1: Update the failing shape + error tests**

In `tests/dashboard/test_aggregator.py`, in `test_build_assembles_happy_path_payload`, change the assertion set and add a projects-shape assertion:

```python
    out = DashboardAggregator(store).build()
    assert set(out) >= {"generated_at", "kpis", "timeline", "tables", "review_questions", "projects"}
    assert out["tables"]["spending_by_dept"][0]["department"] == "Codes"
    assert set(out["projects"]) == {"projects", "administrative", "counts", "funding_in_flight"}
```

In `test_build_never_raises_records_errors`, add `"projects"` to the expected error-key set:

```python
    assert set(out["errors"].keys()) == {
        "kpis", "timeline", "tables", "departments", "resolutions",
        "goals", "legislation", "meetings", "budget", "vacancies",
        "votes", "metrics", "vendor_spend", "commitments", "review_questions", "projects",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/dashboard/test_aggregator.py -q`
Expected: FAIL — `projects` missing from `set(out)` / from the errors set.

- [ ] **Step 3: Add `_build_projects` and register the panel**

In `src/dashboard/aggregator.py`, add the method next to `_build_review_questions`:

```python
    # -- Projects (deterministic live layer over grants + resolutions; NO LLM) --
    def _build_projects(self) -> dict:
        from src.dashboard.projects import Projects
        return Projects(self.sql, now=self.now).build()
```

In `build()`, add the panel after the `review_questions` line:

```python
            "review_questions": self._safe("review_questions", self._build_review_questions, errors),
            "projects": self._safe("projects", self._build_projects, errors),
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/dashboard/ -q`
Expected: PASS (all dashboard tests).

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): register projects panel in build()"
```

---

### Task 3: Projects tab (frontend in `templates/redesign.html`)

**Files:**
- Modify: `templates/redesign.html` (nav item, view container, `state`, `CRUMB`, `render` map + dispatch + `dataTab`, `renderProjects()`, `openProject()`)

**Interfaces:**
- Consumes: `D.projects` = `{projects, administrative, counts, funding_in_flight}` from Task 2; existing helpers `esc`, `cell`, `money`, `dkey`, `deptDisplay`; the `#dossier` / `#dossier-overlay` slide-over and `closeDossier`.
- Produces: a working "Projects" tab reachable from the sidebar.

- [ ] **Step 1: Add the nav item**

After the `data-tab="money"` nav item (the Finances entry), add:

```html
      <div class="navitem" data-tab="projects"><svg width="17" height="17" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4.5h5l1.5 2H14v6.5a.5.5 0 0 1-.5.5h-11a.5.5 0 0 1-.5-.5V4.5z"/></svg><span>Projects</span></div>
```

- [ ] **Step 2: Add the view container**

Immediately before `<!-- NEXT QUARTER (generated questions) -->`, add:

```html
      <!-- PROJECTS -->
      <div class="view" id="v-projects">
        <div style="margin-bottom:20px"><h1 class="page">Projects</h1><p class="psub">Grant-funded initiatives and council-authorized work — land development, contracts, and grant actions — with where each one stands. Updates automatically as new resolutions are added.</p></div>
        <div id="proj-kpis" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px"></div>
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px;flex-wrap:wrap">
          <select class="filter" id="proj-type" onchange="state.projType=this.value;renderProjects()"></select>
          <select class="filter" id="proj-dept" onchange="state.projDept=this.value;renderProjects()"></select>
          <select class="filter" id="proj-status" onchange="state.projStatus=this.value;renderProjects()"></select>
          <input id="proj-search" class="txt-input" placeholder="Search projects…" style="flex:1;min-width:180px" oninput="state.projSearch=this.value;renderProjects()">
          <span id="proj-count" style="font-size:12.5px;color:#8a867d;white-space:nowrap"></span>
        </div>
        <div id="proj-list" style="display:flex;flex-direction:column;gap:10px"></div>
        <div id="proj-admin" style="margin-top:26px"></div>
      </div>
```

- [ ] **Step 3: Register state, crumb, view map, dispatch, dataTab**

In the `state` object literal, add the project filter fields:

```javascript
let state = { tab:'ask', money:'budget', dept:'All', mtg:null, deptName:null, goalsDept:'All', goalsPeriod:'All', goalsSearch:'', activitySeg:'timeline', activityStage:'all', mtgStage:'all', qDept:null, projType:'All', projDept:'All', projStatus:'All', projSearch:'' };
```

In `CRUMB`, add the `projects` entry (after `money`):

```javascript
money:['Explore','Finances'],projects:['Explore','Projects'],
```

In `render()`'s `map`, add `projects`:

```javascript
  const map={ask:'v-ask',overview:'v-overview',activity:'v-activity',money:'v-money',projects:'v-projects',questions:'v-questions',meeting:'v-meeting',goals:'v-goals',people:'v-people',documents:'v-documents',add:'v-add'};
```

In the data-view re-render wrapper, add `'projects'` to `dataTab` and a dispatch line:

```javascript
  const dataTab=['overview','activity','money','projects','questions','goals','people'].includes(state.tab);
```
```javascript
  if(state.tab==='questions') renderQuestions();
  if(state.tab==='projects') renderProjects();
```

- [ ] **Step 4: Add `renderProjects()` and `openProject()`**

Immediately before `// ---- next quarter: generated questions ----`, add:

```javascript
// ---- projects: live layer over grants + resolutions ----
const PROJ_TYPE={grant:{l:'Grant-funded',c:'#2d6a4f'},land_development:{l:'Land development',c:'#16344f'},grant_action:{l:'Grant action',c:'#0f6e63'},contract:{l:'Contract',c:'#c4691e'},budget:{l:'Budget',c:'#6f6b64'},appointment:{l:'Appointment',c:'#6f6b64'},other:{l:'Other',c:'#8a867d'}};
const PROJ_STATUS_PILL={Active:'p-green',Awarded:'p-teal',Proposed:'p-grey',Completed:'p-green',Closed:'p-grey',Stalled:'p-red'};
let _projIndex={};
function projTag(t){ const x=PROJ_TYPE[t]||PROJ_TYPE.other; return `<span style="font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:${x.c}">${x.l}</span>`; }
function projStatusPill(s){ return `<span class="pill ${PROJ_STATUS_PILL[s]||'p-grey'}" style="flex-shrink:0">${esc(s||'—')}</span>`; }
function projRow(p){
  return `<div class="card proj-row" data-id="${esc(p.id)}" style="padding:14px 18px;display:flex;align-items:center;gap:16px;cursor:pointer">
    <div style="flex:1;min-width:0">
      <div style="display:flex;align-items:center;gap:9px;margin-bottom:4px">${projTag(p.type)}${p.attention?'<span class="pill p-amber" style="flex-shrink:0">Needs attention</span>':''}</div>
      <div style="font-weight:600;font-size:14px;line-height:1.35">${cell(p.title)}</div>
      <div style="font-size:12px;color:#8a867d;margin-top:3px">${cell(p.department)}${p.party?' · '+esc(p.party):''}</div>
    </div>
    <div style="text-align:right;flex-shrink:0"><div class="mono" style="font-size:13px;color:#16344f">${p.amount!=null?money(p.amount):''}</div><div style="margin-top:4px">${projStatusPill(p.status)}</div></div>
    <span style="color:#c8c3b8;font-size:18px;flex-shrink:0">›</span></div>`;
}
function renderProjects(){
  const P=D.projects||{projects:[],administrative:[],counts:{},funding_in_flight:0};
  const all=P.projects||[], admin=P.administrative||[], c=P.counts||{};
  _projIndex={}; all.concat(admin).forEach(p=>{ _projIndex[p.id]=p; });
  // KPIs
  const kpis=[
    ['Active projects', c.active!=null?c.active:0, '#16344f'],
    ['Funding in flight', money(P.funding_in_flight||0), '#0f6e63'],
    ['Need attention', c.attention!=null?c.attention:0, (c.attention?'#c4691e':'#16344f')],
    ['Total tracked', all.length, '#16344f'],
  ];
  $('#proj-kpis').innerHTML=kpis.map(([l,v,b])=>`<div class="kpi"><div class="bar" style="background:${b}"></div><div class="lbl">${l}</div><div class="val">${v}</div></div>`).join('');
  // filter dropdowns
  const typeVal=state.projType||'All', depVal=state.projDept||'All', stVal=state.projStatus||'All', q=(state.projSearch||'').trim().toLowerCase();
  const types=[...new Set(all.map(p=>p.type))];
  $('#proj-type').innerHTML=`<option value="All">All types</option>`+types.map(t=>`<option value="${t}"${typeVal===t?' selected':''}>${(PROJ_TYPE[t]||PROJ_TYPE.other).l}</option>`).join('');
  const depKeys={}; all.forEach(p=>{ if(p.department){ const k=dkey(p.department); (depKeys[k]=depKeys[k]||[]).push(p.department); } });
  const depOpts=Object.keys(depKeys).map(k=>({k,name:deptDisplay(k,depKeys[k])})).sort((a,b)=>a.name.localeCompare(b.name));
  $('#proj-dept').innerHTML=`<option value="All">All departments</option>`+depOpts.map(d=>`<option value="${esc(d.name)}"${depVal!=='All'&&dkey(depVal)===d.k?' selected':''}>${esc(d.name)}</option>`).join('');
  const statuses=[...new Set(all.map(p=>p.status))];
  $('#proj-status').innerHTML=`<option value="All">All statuses</option>`+statuses.map(s=>`<option value="${s}"${stVal===s?' selected':''}>${s}</option>`).join('');
  // filter
  let list=all.slice();
  if(typeVal!=='All') list=list.filter(p=>p.type===typeVal);
  if(depVal!=='All') list=list.filter(p=>dkey(p.department)===dkey(depVal));
  if(stVal!=='All') list=list.filter(p=>p.status===stVal);
  if(q) list=list.filter(p=>((p.title||'')+' '+(p.party||'')+' '+(p.department||'')).toLowerCase().includes(q));
  $('#proj-count').textContent=`${list.length} project${list.length===1?'':'s'}`;
  $('#proj-list').innerHTML=list.length?list.map(projRow).join(''):'<div class="empty">No projects match these filters.</div>';
  $('#proj-list').querySelectorAll('.proj-row').forEach(el=>el.onclick=()=>openProject(el.dataset.id));
  // administrative (collapsed section)
  $('#proj-admin').innerHTML = admin.length?`<div style="font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#a7a298;margin-bottom:10px">Administrative resolutions (${admin.length})</div><div style="display:flex;flex-direction:column;gap:10px">${admin.map(projRow).join('')}</div>`:'';
  $('#proj-admin').querySelectorAll('.proj-row').forEach(el=>el.onclick=()=>openProject(el.dataset.id));
}
function openProject(id){
  const p=_projIndex[id]; if(!p) return;
  const x=PROJ_TYPE[p.type]||PROJ_TYPE.other;
  const line=(lbl,val)=>val?`<div style="display:flex;justify-content:space-between;gap:14px;padding:11px 0;border-top:1px solid rgba(28,27,25,.07)"><span style="font-size:12px;color:#8a867d">${lbl}</span><span style="font-size:13px;font-weight:500;text-align:right">${val}</span></div>`:'';
  $('#dossier').innerHTML=`
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:18px">
      <div><div style="margin-bottom:8px">${projTag(p.type)}</div><h2 class="serif" style="font-size:22px;font-weight:600;color:#16344f;line-height:1.2">${cell(p.title)}</h2></div>
      <button id="dossier-close" class="btn-ghost" style="padding:6px 11px;flex-shrink:0" aria-label="Close">✕</button>
    </div>
    <div style="margin-bottom:8px">${projStatusPill(p.status)}${p.attention?' <span class="pill p-amber">Needs attention</span>':''}</div>
    ${line('Department',cell(p.department))}
    ${line('Party',p.party?esc(p.party):'')}
    ${line('Amount',p.amount!=null?money(p.amount):'')}
    ${line('Date',p.date?esc(p.date):'')}
    ${line('End date',p.end_date?esc(p.end_date):'')}
    ${line('Resolution',p.resolution_number?esc(p.resolution_number):'')}
    ${line('Source',p.source_file?esc(p.source_file):'')}`;
  $('#dossier-close').onclick=closeDossier;
  $('#dossier').classList.add('open'); $('#dossier-overlay').classList.add('open');
}
```

- [ ] **Step 5: Verify Jinja + JS parse**

Run:
```bash
PYTHONPATH=. python3 -c "from jinja2 import Environment,FileSystemLoader; Environment(loader=FileSystemLoader('templates')).get_template('redesign.html'); print('JINJA OK')"
PYTHONPATH=. python3 - <<'PY'
import re,subprocess,tempfile,os
html=open('templates/redesign.html').read()
body='\n;\n'.join(re.findall(r'<script[^>]*>(.*?)</script>',html,re.S))
f=tempfile.NamedTemporaryFile('w',suffix='.js',delete=False); f.write(body); f.close()
r=subprocess.run(['node','--check',f.name],capture_output=True,text=True)
print('JS OK' if r.returncode==0 else 'JS FAIL:\n'+r.stderr); os.unlink(f.name)
PY
```
Expected: `JINJA OK` and `JS OK`.

- [ ] **Step 6: Verify live (free — no LLM)**

Run: `bash run.sh` (background), wait for health, then:
```bash
curl -s http://localhost:5001/ | grep -o 'data-tab="projects"'
curl -s "http://localhost:5001/dashboard/data?fresh=1" | PYTHONPATH=. python3 -c "import sys,json; p=json.load(sys.stdin)['projects']; print('keys:',sorted(p)); print('projects:',len(p['projects']),'admin:',len(p['administrative']),'by_type:',p['counts']['by_type'])"
```
Expected: the nav tab present; `projects` payload with non-empty `projects` (grants populate it) and a `by_type` including `grant`.

- [ ] **Step 7: Commit**

```bash
git add templates/redesign.html
git commit -m "feat(ui): Projects tab — initiative lens over grants + resolutions"
```

---

## Self-Review

**Spec coverage:**
- Data layer `src/dashboard/projects.py` (assembly, classification, status) → Task 1. ✓
- Wired into `build()` via `_safe()` as `projects` panel → Task 2. ✓
- Projects tab (KPIs, type/department/status/search filters, list, slide-over detail, administrative bucket) → Task 3. ✓
- Live/auto-update (no ingest step) → inherent in Task 1 (built every `build()`); verified Step 6. ✓
- Zero LLM cost → no model calls anywhere in Tasks 1–3. ✓
- Grant/resolution kept separate (no fuzzy merge) → Task 1 emits both sources as distinct records. ✓
- Grants stay in Finances (unchanged) → no edits to `renderMoney`. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `Projects(sql_store, now=None).build()` returns `{projects, administrative, counts, funding_in_flight}`; each record's keys (`id, source, type, title, department, party, amount, status, date, end_date, source_file, resolution_number, attention`) are produced in Task 1 and consumed verbatim in Task 3 (`projRow`, `openProject`, filters) and asserted in Task 2's shape test. `classify_resolution` / `normalize_grant_status` / `normalize_resolution_status` names match across module and tests. ✓

**Note:** the spec's project-record list did not enumerate `attention`; it is added here as an internal field powering the "needs attention" KPI and sort (documented in Task 1's Produces block). No spec requirement is left without a task.
