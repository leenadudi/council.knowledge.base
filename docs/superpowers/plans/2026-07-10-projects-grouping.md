# Projects Grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse sibling project cards (same vendor + type within a department) into one expandable group in the Projects tab.

**Architecture:** Backend tags each resolution-derived project with a deterministic `group_key`; the front end clusters by that key at render time. The flat `projects` list, filters, KPI counts, and search are unchanged.

**Tech Stack:** Python 3.14, pytest (backend); vanilla JS in a Flask Jinja template (front end).

## Global Constraints

- Deterministic only — no LLM calls anywhere in this feature.
- Backend logic under `src/`, tests under `tests/`, `pythonpath = .` (pytest.ini).
- No change to `Projects.build()`'s existing return keys (`projects`, `administrative`, `counts`, `funding_in_flight`) — only an additive per-item `group_key` field.
- Front-end change is confined to `templates/redesign.html`'s `renderProjects` path; `projRow`/`openProject`/`_projIndex` stay working for individual rows.

---

### Task 1: Backend — tag projects with a deterministic `group_key`

**Files:**
- Modify: `src/dashboard/projects.py` (add `_normalize_party`; set `group_key` in `build()`)
- Test: `tests/dashboard/test_projects_grouping.py`

**Interfaces:**
- Produces: `_normalize_party(vendor: str) -> str`; each resolution-derived project dict gains `group_key: str | None` (= `f"{_normalize_party(party)}|{type}"` when it has a vendor, else `None`). Grants and administrative items get `group_key = None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/dashboard/test_projects_grouping.py
import datetime
from contextlib import contextmanager

from src.dashboard.projects import Projects, _normalize_party


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


def test_normalize_party_collapses_variants():
    a = _normalize_party("Lamar Advantage GP Company, LLC")
    b = _normalize_party("LAMAR ADVANTAGE GP COMPANY LLC")
    c = _normalize_party("  Lamar Advantage GP Company , llc. ")
    assert a == b == c and a != ""


def _lamar(rn, loc):
    return {"resolution_number": rn,
            "title": f"A Resolution authorizing a lease agreement for a billboard {loc}",
            "vendor": "Lamar Advantage GP Company, LLC", "amount": 13000.0,
            "department": "Mayor", "adopted_date": datetime.date(2026, 2, 1), "status": "Passed",
            "source_file": f"{rn}.pdf"}


def test_build_assigns_shared_group_key_to_siblings():
    now = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "BUILD Grant", "department": "Public Works", "amount": 5.0,
             "start_date": None, "end_date": None, "status": "awarded", "source_file": "g.pdf"},
        ],
        "FROM resolutions": [
            _lamar("10-2026", "on I-83"),
            _lamar("11-2026", "on Paxton St"),
            {"resolution_number": "22-2026",
             "title": "A Resolution authorizing a professional services agreement",
             "vendor": "McCormick Law Firm", "amount": None, "department": "Mayor",
             "adopted_date": datetime.date(2026, 3, 2), "status": "Passed", "source_file": "22.pdf"},
        ],
    })
    out = Projects(store, now=now).build()
    by_num = {p["resolution_number"]: p for p in out["projects"] if p.get("resolution_number")}
    # the two Lamar leases share one key
    assert by_num["10-2026"]["group_key"] == by_num["11-2026"]["group_key"]
    # a different vendor differs
    assert by_num["22-2026"]["group_key"] != by_num["10-2026"]["group_key"]
    # grants are never grouped
    grant = next(p for p in out["projects"] if p["source"] == "grant")
    assert grant["group_key"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/dashboard/test_projects_grouping.py -v`
Expected: FAIL — `ImportError: cannot import name '_normalize_party'`

- [ ] **Step 3: Write minimal implementation**

In `src/dashboard/projects.py`, add near the top-level helpers (after `_iso`, ~line 64):

```python
_PARTY_SUFFIX_RE = re.compile(r"[,\.]?\s*\b(llc|l\.l\.c|inc|incorporated|co|company|corp|corporation|lp|llp|pllc)\b\.?", re.I)


def _normalize_party(vendor: str) -> str:
    """Canonicalize a vendor/party string so trivial variants group together:
    lowercase, drop trailing corporate suffixes/punctuation, collapse whitespace."""
    s = (vendor or "").lower()
    s = _PARTY_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())
```

In the grants loop in `build()` (the `projects.append({...})` for grants, ~line 108), add:

```python
                "attention": expiring or status == "Stalled",
                "group_key": None,
```

In the resolutions loop in `build()` (the `rec = {...}` dict, ~line 124), add a `group_key`:

```python
            party = r.get("vendor") or None
            rec = {
                "id": f"res-{r.get('resolution_number')}", "source": "resolution", "type": typ,
                "title": title, "department": r.get("department"),
                "party": party, "amount": amount, "status": status,
                "date": _iso(r.get("adopted_date")), "end_date": None,
                "source_file": r.get("source_file"), "resolution_number": r.get("resolution_number"),
                "attention": status == "Stalled",
                "group_key": (f"{_normalize_party(party)}|{typ}" if party else None),
            }
```

(Administrative items go through this same `rec`; that's fine — they carry a `group_key` but the front end renders the Administrative section flat and never clusters it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/dashboard/test_projects_grouping.py tests/dashboard/test_projects.py -v`
Expected: PASS (new grouping tests pass; existing `test_projects.py` still passes)

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/projects.py tests/dashboard/test_projects_grouping.py
git commit -m "feat(projects): tag projects with deterministic group_key"
```

---

### Task 2: Front end — cluster sibling cards into an expandable group

**Files:**
- Modify: `templates/redesign.html` (`renderProjects` ~line 817; add `renderProjItems`, `projGroupCard`, `mostCommon`)
- Verify: run the app (no JS test harness in this repo)

**Interfaces:**
- Consumes: each project's `group_key` (Task 1). `projRow(p)`, `projTag`, `projStatusPill`, `PROJ_TYPE`, `money`, `cell`, `openProject` already exist.

- [ ] **Step 1: Add the clustering helpers**

Immediately after the `projRow` function (ends ~line 816), add:

```javascript
function mostCommon(arr){ const c={}; let best=arr[0], n=0; arr.forEach(x=>{ c[x]=(c[x]||0)+1; if(c[x]>n){ n=c[x]; best=x; } }); return best; }

let _pgSeq=0;
// Render a department's items: cluster by group_key; a key with >=2 members collapses.
function renderProjItems(items){
  const groups={}, order=[];
  items.forEach(p=>{
    const k=p.group_key;
    if(!k){ order.push({single:p}); return; }
    if(!groups[k]){ groups[k]={key:k, items:[]}; order.push({group:groups[k]}); }
    groups[k].items.push(p);
  });
  return order.map(o=>{
    if(o.single) return projRow(o.single);
    if(o.group.items.length<2) return projRow(o.group.items[0]);
    return projGroupCard(o.group, 'pg'+(_pgSeq++));
  }).join('');
}

function projGroupCard(g, gid){
  const first=g.items[0];
  const vendor=first.party||first.title;
  const type=first.type, n=g.items.length;
  const label=(PROJ_TYPE[type]||PROJ_TYPE.other).l;
  const amounts=g.items.map(p=>p.amount).filter(a=>a!=null);
  const sum=amounts.length?amounts.reduce((s,a)=>s+a,0):null;
  const anyAttn=g.items.some(p=>p.attention);
  const status=g.items.some(p=>p.status==='Active')?'Active':mostCommon(g.items.map(p=>p.status));
  return `<div class="card" style="padding:0;overflow:hidden">
    <div class="proj-group-head" data-gid="${gid}" style="padding:14px 18px;display:flex;align-items:center;gap:16px;cursor:pointer">
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:9px;margin-bottom:5px">${projTag(type)}<span style="font-size:11px;font-weight:600;color:#6f6b64;background:rgba(28,27,25,.06);border-radius:5px;padding:2px 7px">${label} ×${n}</span>${anyAttn?'<span class="pill p-amber">Needs attention</span>':''}</div>
        <div style="font-weight:600;font-size:14px">${cell(vendor)}</div>
      </div>
      <div style="text-align:right;flex-shrink:0"><div class="mono" style="font-size:13px;color:#16344f">${sum!=null?money(sum):''}</div><div style="margin-top:4px">${projStatusPill(status)}</div></div>
      <span class="proj-group-chev" style="color:#c8c3b8;font-size:18px;flex-shrink:0;transition:transform .15s">›</span>
    </div>
    <div class="proj-group-body" id="${gid}" style="display:none;flex-direction:column;gap:10px;padding:0 12px 12px">${g.items.map(projRow).join('')}</div>
  </div>`;
}
```

- [ ] **Step 2: Route both render branches through `renderProjItems`**

In `renderProjects` (~line 844-854), replace the department-grouped branch's item render and the single-department branch:

Change the `depVal==='All'` map body from:
```javascript
      + `<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:10px">${g.items.map(projRow).join('')}</div>`
```
to:
```javascript
      + `<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:10px">${renderProjItems(g.items)}</div>`
```

Change the `else` (single-department) branch from:
```javascript
    $('#proj-list').innerHTML=list.map(projRow).join('');
```
to:
```javascript
    $('#proj-list').innerHTML=renderProjItems(list);
```

Reset the sequence counter at the top of `renderProjects` (right after `const all=...` line ~819):
```javascript
   _pgSeq=0;
```

- [ ] **Step 3: Bind the group expand/collapse toggle**

In `renderProjects`, right after the existing member-row binding line (`$('#proj-list').querySelectorAll('.proj-row').forEach(el=>el.onclick=()=>openProject(el.dataset.id));`, ~line 855), add:

```javascript
   $('#proj-list').querySelectorAll('.proj-group-head').forEach(el=>el.onclick=()=>{
     const body=document.getElementById(el.dataset.gid);
     const open=body.style.display!=='none';
     body.style.display=open?'none':'flex';
     const chev=el.querySelector('.proj-group-chev'); if(chev) chev.style.transform=open?'':'rotate(90deg)';
   });
```

(The member `.proj-row` elements inside a group body are already in the DOM, so the existing `openProject` binding above covers them — each member still opens its own dossier.)

- [ ] **Step 4: Verify in the running app**

```bash
bash run.sh   # serves http://127.0.0.1:5001 ; restart if already running
```
Then open http://127.0.0.1:5001/dashboard → Projects tab, department "Mayor / Administration":
- Expected: the five Lamar Advantage leases now render as ONE card titled "Lamar Advantage GP Company, LLC" with a "Contract ×5" badge and a summed amount; clicking it expands to five member rows; each member row opens its own dossier.
- Expected: singleton projects (e.g. McCormick Law Firm) and the Administrative section look unchanged.
- Confirm no console errors (the page's data comes from `/dashboard/data`, which needs no LLM).

- [ ] **Step 5: Commit**

```bash
git add templates/redesign.html
git commit -m "feat(projects): cluster sibling project cards into expandable groups"
```

---

## Self-Review

**Spec coverage:**
- Deterministic `group_key` = normalized vendor + type; grants null → Task 1 ✓
- Front-end clustering within department, ≥2 collapses, singletons unchanged → Task 2 ✓
- Group card shows vendor + "type ×N" + summed amount + rolled-up status + attention → Task 2 `projGroupCard` ✓
- Expand reveals member rows, each opens its dossier → Task 2 Step 3 ✓
- Filters/counts/search unchanged (operate on flat `all`) → untouched in Task 2 ✓
- No LLM, no migration → both tasks ✓

**Placeholder scan:** none — all steps carry real code/commands.

**Type consistency:** `group_key` produced in Task 1 and consumed in Task 2; `renderProjItems`/`projGroupCard`/`mostCommon`/`_pgSeq` defined and used consistently; reuses existing `projRow`/`projTag`/`projStatusPill`/`PROJ_TYPE`/`money`/`cell`/`openProject`.
