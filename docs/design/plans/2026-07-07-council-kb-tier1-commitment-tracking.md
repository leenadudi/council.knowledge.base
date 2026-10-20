# Council KB — Tier 1 + Commitment Tracking v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the extracted-but-dormant council data (votes, metrics, quarter trends, vendor spend) and add department-level commitment tracking (authorized vs. actual, expiring grants), entirely with SQL + UI — no new extraction or LLM calls.

**Architecture:** All new data is computed in `src/dashboard/aggregator.py` as `_build_*` methods registered into `DashboardAggregator.build()`, served unchanged through the existing cached `/dashboard/data` route, and rendered in `templates/redesign.html` (vanilla JS, inline styles) following the existing design system. Backend methods are unit-tested with the repo's `_FakeStore` substring-mock pattern; frontend is verified manually against the running app.

**Tech Stack:** Python 3.14 / Flask, psycopg2 (RealDictCursor), PostgreSQL (+pgvector), pytest, vanilla JS + inline SVG (no chart library — CSP-safe, matches existing `.barbg` bars).

## Global Constraints

- **No LLM / ingestion spend.** Every task is read-only SQL + presentation. Do not call the ingestion pipeline, the query pipeline, `/ask`, or any Anthropic/Voyage API. (Per project memory `funds-ask-before-llm-spend`.)
- **Preserve all existing element IDs, classes, and JS bindings** in `templates/redesign.html`. Additive changes only.
- **Money coercion:** every `Decimal` column read from Postgres must be coerced to `float` before returning (JSON can't serialize `Decimal`). Follow the existing `float(x) if x is not None else None` pattern — never coerce `None` to `0.0`.
- **Department grouping:** whenever grouping by department across tables, use `DashboardAggregator._dept_key()` so name variants merge (e.g. "Bureau of Fire" == "Fire").
- **Tests must not require a live DB.** Use the `_FakeStore`/`_FakeCursor` pattern already in `tests/dashboard/test_aggregator.py`. Mark any live-DB test `@pytest.mark.integration`.
- **Run tests with:** `python3 -m pytest tests/dashboard/test_aggregator.py -v -m "not integration"`.

---

## File Structure

- **Modify** `src/dashboard/aggregator.py` — add `_build_votes`, `_build_metrics`, `_build_vendor_spend`, `_build_commitments`; register them (+ restore `_build_timeline`) in `build()`. All new methods follow the existing one-method-per-panel pattern.
- **Modify** `tests/dashboard/test_aggregator.py` — repair 3 stale tests; add one unit test per new builder; extend the build-shape test.
- **Modify** `templates/redesign.html` — add nav items, `<div class="view">` panels, and `render*()` functions for trends, votes, metrics, and follow-through; register them in the `render()` dispatch, `CRUMB` map, and `map` object.

No new files. No new dependencies (verified against `requirements.txt` — psycopg2, Flask, pydantic already present; no chart lib added).

---

## Task 0: Green baseline — repair stale tests + restore the `timeline` panel

**Files:**
- Modify: `src/dashboard/aggregator.py:396-412` (the `build()` method)
- Modify: `tests/dashboard/test_aggregator.py` (3 failing tests)

**Interfaces:**
- Produces: `build()` output now contains key `"timeline"` → the dict returned by the existing `_build_timeline()` (`{"grants":[...], "reports":[...], "resolutions":[...], "spending":[...]}`).

- [ ] **Step 1: Confirm the baseline is red**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py -v -m "not integration"`
Expected: `3 failed, 7 passed` — failures in `test_build_assembles_happy_path_payload`, `test_build_never_raises_records_errors`, `test_build_departments_shape`.

- [ ] **Step 2: Restore `timeline` to `build()`**

In `src/dashboard/aggregator.py`, edit the `out` dict in `build()` to add the `timeline` line immediately after `kpis`:

```python
        out = {
            "generated_at": self.now.isoformat(),
            "kpis": self._safe("kpis", self._build_kpis, errors),
            "timeline": self._safe("timeline", self._build_timeline, errors),
            "tables": self._safe("tables", self._build_tables, errors),
            "departments": self._safe("departments", self._build_departments, errors),
            "resolutions": self._safe("resolutions", self._build_resolutions, errors),
            "goals": self._safe("goals", self._build_goals, errors),
            "legislation": self._safe("legislation", self._build_legislation, errors),
            "meetings": self._safe("meetings", self._build_meetings, errors),
            "budget": self._safe("budget", self._build_budget, errors),
            "vacancies": self._safe("vacancies", self._build_vacancies, errors),
        }
```

- [ ] **Step 3: Repair `test_build_assembles_happy_path_payload`**

Replace the whole test with a fake that also feeds the timeline queries (`FROM grants WHERE start_date`, `FROM documents WHERE year`, `FROM resolutions WHERE adopted_date`, `GROUP BY year, quarter`). Note the substring keys are chosen so each maps to exactly one query:

```python
def test_build_assembles_happy_path_payload():
    store = _FakeStore({
        "FROM grants WHERE (LOWER(status)": [{"active": 0, "expiring": 0}],
        "SUM(amount) AS funds": [{"funds": 0.0}],
        "FROM expenditures WHERE line_item": [{"ytd": 0, "budget": 0}],
        "MAX(year)": [{"year": None}],
        "COUNT(*) AS c FROM resolutions": [{"c": 0}],
        "document_type='unclassified'": [{"c": 0}],
        # timeline queries
        "FROM grants WHERE start_date": [],
        "FROM documents\n": [],
        "FROM resolutions WHERE adopted_date": [],
        "GROUP BY year, quarter": [],
        # tables
        "FROM grants ORDER BY start_date": [],
        "GROUP BY department ORDER BY ytd_expended": [{"department": "Codes", "revised_budget": 1.0, "ytd_expended": 0.5}],
        "FROM documents ORDER BY": [],
    })
    out = DashboardAggregator(store).build()
    assert set(out) >= {"generated_at", "kpis", "timeline", "tables"}
    assert out["tables"]["spending_by_dept"][0]["department"] == "Codes"
```

- [ ] **Step 4: Repair `test_build_never_raises_records_errors`**

The `_BoomStore` makes every panel fail; assert the current full panel set (which now includes `timeline` and all Task-5 panels are NOT yet added, so list exactly today's panels):

```python
def test_build_never_raises_records_errors():
    out = DashboardAggregator(_BoomStore()).build()
    assert out["kpis"] is None and out["timeline"] is None and out["tables"] is None
    assert out["departments"] is None and out["resolutions"] is None
    assert set(out["errors"].keys()) == {
        "kpis", "timeline", "tables", "departments", "resolutions",
        "goals", "legislation", "meetings", "budget", "vacancies",
    }
```

- [ ] **Step 5: Repair `test_build_departments_shape`**

The current `_build_departments()` runs (1) `... FROM documents WHERE ... document_type = ANY(%s)` and (2) `... AS rb ... FROM expenditures ... GROUP BY department`. Rewrite the fake to match:

```python
def test_build_departments_shape():
    store = _FakeStore({
        "document_type = ANY": [
            {"department": "Codes", "document_type": "quarterly_report"},
            {"department": "Fire", "document_type": "budget"},
        ],
        "AS rb": [{"department": "Codes", "rb": 100.0, "ytd": 40.0}],
    })
    depts = DashboardAggregator(store)._build_departments()
    codes = next(d for d in depts if d["department"] == "Codes")
    assert codes["revised_budget"] == 100.0 and codes["ytd_expended"] == 40.0 and codes["report_count"] == 1
    fire = next(d for d in depts if d["department"] == "Fire")
    assert fire["revised_budget"] == 0 and fire["report_count"] == 0   # budget doc, not a quarterly_report
```

- [ ] **Step 6: Run the suite — expect all green**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py -v -m "not integration"`
Expected: `10 passed`.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "fix(dashboard): restore timeline panel to build() and repair stale aggregator tests"
```

---

## Task 1: `_build_votes` — roll-call by resolution + per-member voting records

**Files:**
- Modify: `src/dashboard/aggregator.py` (add method after `_build_resolutions`, ~line 285)
- Test: `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Produces: `_build_votes() -> dict`:
  ```
  {
    "by_resolution": [ {"resolution_number": str,
                        "title": str|None, "amount": float|None, "status": str|None,  # JOINED from resolutions
                        "tally": {"yea":int,"nay":int,"abstain":int,"absent":int,"other":int},
                        "votes": [{"member": str, "vote": str}]} ],   # sorted by resolution_number
    "by_member":     [ {"member": str, "total":int,
                        "yea":int,"nay":int,"abstain":int,"absent":int,"other":int} ]  # sorted by total desc
  }
  ```

- [ ] **Step 1: Write the failing test**

```python
def test_build_votes_rollcall_and_member_records():
    import decimal
    # rows come pre-joined to resolutions (title/amount/status repeat per resolution)
    store = _FakeStore({"FROM votes": [
        {"resolution_number": "9-2026", "council_member": "Wanda Williams", "vote": "Yea",
         "title": "BUILD Grant", "amount": decimal.Decimal("3000000"), "status": "Passed"},
        {"resolution_number": "9-2026", "council_member": "Danielle Bowers", "vote": "Nay",
         "title": "BUILD Grant", "amount": decimal.Decimal("3000000"), "status": "Passed"},
        {"resolution_number": "9-2026", "council_member": "Ralph Rodriguez", "vote": "abstain",
         "title": "BUILD Grant", "amount": decimal.Decimal("3000000"), "status": "Passed"},
        {"resolution_number": "10-2026", "council_member": "Wanda Williams", "vote": "YES",
         "title": None, "amount": None, "status": None},
        {"resolution_number": "10-2026", "council_member": "Danielle Bowers", "vote": "absent",
         "title": None, "amount": None, "status": None},
    ]})
    v = DashboardAggregator(store)._build_votes()

    r9 = next(r for r in v["by_resolution"] if r["resolution_number"] == "9-2026")
    assert r9["tally"] == {"yea": 1, "nay": 1, "abstain": 1, "absent": 0, "other": 0}
    assert r9["title"] == "BUILD Grant" and r9["amount"] == 3000000.0 and r9["status"] == "Passed"
    assert {x["member"] for x in r9["votes"]} == {"Wanda Williams", "Danielle Bowers", "Ralph Rodriguez"}

    williams = next(m for m in v["by_member"] if m["member"] == "Wanda Williams")
    assert williams["total"] == 2 and williams["yea"] == 2   # "Yea" and "YES" both bucket to yea
    # by_member sorted by total desc; Williams (2) before anyone with fewer
    assert v["by_member"][0]["total"] >= v["by_member"][-1]["total"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_votes_rollcall_and_member_records -v`
Expected: FAIL with `AttributeError: 'DashboardAggregator' object has no attribute '_build_votes'`.

- [ ] **Step 3: Write the implementation**

Add to `src/dashboard/aggregator.py` (after `_build_resolutions`). Also add the module-level bucket helper near the top constants (after `_ACTIVE_STATUSES`):

```python
_VOTE_BUCKETS = {
    "yea": "yea", "yes": "yea", "aye": "yea", "y": "yea", "for": "yea", "in favor": "yea",
    "nay": "nay", "no": "nay", "n": "nay", "against": "nay",
    "abstain": "abstain", "abstained": "abstain", "abstention": "abstain",
    "absent": "absent", "away": "absent",
}


def _vote_bucket(vote: str) -> str:
    return _VOTE_BUCKETS.get((vote or "").strip().lower(), "other")
```

```python
    # -- Votes (roll-call + member records, joined to resolutions) ------------
    def _build_votes(self) -> dict:
        with self.sql.cursor() as cur:
            # JOIN so each roll-call carries what the resolution actually was,
            # not just its number. LEFT JOIN keeps votes whose resolution row is missing.
            cur.execute("SELECT v.resolution_number, v.council_member, v.vote, "
                        "r.title, r.amount, r.status FROM votes v "
                        "LEFT JOIN resolutions r ON r.resolution_number = v.resolution_number "
                        "WHERE v.council_member IS NOT NULL "
                        "ORDER BY v.resolution_number, v.council_member")
            rows = [dict(r) for r in cur.fetchall()]

        empty_tally = {"yea": 0, "nay": 0, "abstain": 0, "absent": 0, "other": 0}
        by_res: dict = {}
        by_member: dict = {}
        for r in rows:
            rn = r.get("resolution_number") or "—"
            member = r.get("council_member")
            bucket = _vote_bucket(r.get("vote"))

            res = by_res.setdefault(rn, {
                "resolution_number": rn,
                "title": r.get("title"),
                "amount": float(r["amount"]) if r.get("amount") is not None else None,
                "status": r.get("status"),
                "tally": dict(empty_tally), "votes": [],
            })
            res["tally"][bucket] += 1
            res["votes"].append({"member": member, "vote": r.get("vote")})

            m = by_member.setdefault(member, {"member": member, "total": 0, **dict(empty_tally)})
            m["total"] += 1
            m[bucket] += 1

        by_resolution = sorted(by_res.values(), key=lambda x: x["resolution_number"])
        members = sorted(by_member.values(), key=lambda x: -x["total"])
        return {"by_resolution": by_resolution, "by_member": members}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_votes_rollcall_and_member_records -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): _build_votes — roll-call tallies and per-member voting records"
```

---

## Task 2: `_build_metrics` — latest performance metric per department

**Files:**
- Modify: `src/dashboard/aggregator.py` (add method after `_build_goals`)
- Test: `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Produces: `_build_metrics() -> list[dict]`:
  ```
  [ {"department": str,
     "metrics": [{"name": str, "value": float|None, "unit": str|None, "quarter": str, "year": int}]} ]
  # one entry per department (sorted asc); within a department, the latest (year,quarter) value per metric_name
  ```

- [ ] **Step 1: Write the failing test**

```python
def test_build_metrics_keeps_latest_per_metric_and_groups_by_dept():
    import decimal
    store = _FakeStore({"FROM metrics": [
        # ordered latest-first by the query; Python keeps the first seen per (dept, name)
        {"department": "Fire", "metric_name": "Response time", "metric_value": decimal.Decimal("4.20"),
         "metric_unit": "min", "quarter": "Q2", "year": 2026},
        {"department": "Fire", "metric_name": "Response time", "metric_value": decimal.Decimal("5.10"),
         "metric_unit": "min", "quarter": "Q1", "year": 2026},   # older → dropped
        {"department": "Fire", "metric_name": "Calls", "metric_value": decimal.Decimal("1200"),
         "metric_unit": None, "quarter": "Q2", "year": 2026},
    ]})
    out = DashboardAggregator(store)._build_metrics()
    fire = next(d for d in out if d["department"] == "Fire")
    rt = next(m for m in fire["metrics"] if m["name"] == "Response time")
    assert rt["value"] == 4.20 and isinstance(rt["value"], float)   # latest kept, Decimal coerced
    assert {m["name"] for m in fire["metrics"]} == {"Response time", "Calls"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_metrics_keeps_latest_per_metric_and_groups_by_dept -v`
Expected: FAIL with `AttributeError: ... '_build_metrics'`.

- [ ] **Step 3: Write the implementation**

Add after `_build_goals`:

```python
    # -- Metrics (latest performance metric per department) -------------------
    def _build_metrics(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, metric_name, metric_value, metric_unit, quarter, year "
                        "FROM metrics WHERE department IS NOT NULL AND metric_name IS NOT NULL "
                        "ORDER BY year DESC NULLS LAST, quarter DESC NULLS LAST")
            rows = [dict(r) for r in cur.fetchall()]

        seen: set = set()          # (department, metric_name) already captured (latest wins)
        by_dept: dict = {}
        for r in rows:
            dept, name = r["department"], r["metric_name"]
            if (dept, name) in seen:
                continue
            seen.add((dept, name))
            val = r.get("metric_value")
            by_dept.setdefault(dept, []).append({
                "name": name,
                "value": float(val) if val is not None else None,
                "unit": r.get("metric_unit"),
                "quarter": r.get("quarter"),
                "year": int(r["year"]) if r.get("year") is not None else None,
            })
        return [{"department": d, "metrics": m} for d, m in sorted(by_dept.items())]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_metrics_keeps_latest_per_metric_and_groups_by_dept -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): _build_metrics — latest performance metric per department"
```

---

## Task 3: `_build_vendor_spend` — spend aggregated by vendor

**Files:**
- Modify: `src/dashboard/aggregator.py` (add after `_build_metrics`)
- Test: `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Produces: `_build_vendor_spend() -> list[dict]`:
  ```
  [ {"vendor": str, "total": float, "count": int, "departments": [str]} ]   # sorted by total desc
  ```

- [ ] **Step 1: Write the failing test**

```python
def test_build_vendor_spend_aggregates_and_sorts():
    import decimal
    store = _FakeStore({"FROM resolutions\n            WHERE vendor": [
        {"vendor": "USDOT", "amount": decimal.Decimal("3000000"), "department": "Public Works"},
        {"vendor": "USDOT", "amount": decimal.Decimal("500000"), "department": "Engineering"},
        {"vendor": "Acme Paving", "amount": decimal.Decimal("120000"), "department": "Public Works"},
    ]})
    out = DashboardAggregator(store)._build_vendor_spend()
    assert out[0]["vendor"] == "USDOT"                       # highest total first
    assert out[0]["total"] == 3500000.0 and out[0]["count"] == 2
    assert set(out[0]["departments"]) == {"Public Works", "Engineering"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_vendor_spend_aggregates_and_sorts -v`
Expected: FAIL with `AttributeError: ... '_build_vendor_spend'`.

- [ ] **Step 3: Write the implementation**

The SQL uses a newline + `WHERE vendor` so the test's substring key maps to exactly this query (distinct from `_build_resolutions`'s `FROM resolutions ORDER BY`):

```python
    # -- Vendor spend (aggregate council-authorized spend by vendor) ----------
    def _build_vendor_spend(self) -> list[dict]:
        with self.sql.cursor() as cur:
            cur.execute("SELECT vendor, amount, department FROM resolutions\n"
                        "            WHERE vendor IS NOT NULL AND vendor <> '' AND amount IS NOT NULL")
            rows = [dict(r) for r in cur.fetchall()]

        by_vendor: dict = {}
        for r in rows:
            v = by_vendor.setdefault(r["vendor"], {"vendor": r["vendor"], "total": 0.0, "count": 0, "departments": set()})
            v["total"] += float(r["amount"] or 0)
            v["count"] += 1
            if r.get("department"):
                v["departments"].add(r["department"])
        out = [{"vendor": v["vendor"], "total": v["total"], "count": v["count"],
                "departments": sorted(v["departments"])} for v in by_vendor.values()]
        out.sort(key=lambda x: -x["total"])
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_vendor_spend_aggregates_and_sorts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): _build_vendor_spend — council-authorized spend by vendor"
```

---

## Task 4: `_build_commitments` — authorized vs. actual + expiring grants

**Files:**
- Modify: `src/dashboard/aggregator.py` (add after `_build_vendor_spend`)
- Test: `tests/dashboard/test_aggregator.py`

**Interfaces:**
- Produces: `_build_commitments() -> dict`:
  ```
  {
    "authorized_vs_spent": [ {"department": str, "authorized_total": float, "ytd_spend": float} ],  # sorted by authorized_total desc
    "grants_expiring":     [ {"grant_name": str, "department": str|None, "end_date": str,
                              "days_left": int, "amount": float|None} ]   # end_date>=today, within window, sorted days_left asc
  }
  ```
  NOTE (honest scope): `authorized_total` is all-time council resolution $ per department; `ytd_spend` is latest-period YTD expenditure per department. They are shown **side by side as directional context**, NOT as a ratio — there is no FK linking a resolution to its expenditure line (that fuzzy match is deferred to v2). Goal follow-through reuses the existing `goals` panel in the UI (no new backend).

- [ ] **Step 1: Write the failing test**

```python
def test_build_commitments_authorized_actual_and_expiring():
    import datetime, decimal
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "GROUP BY department) AS authorized": [
            {"department": "Public Works", "authorized_total": decimal.Decimal("3500000")},
            {"department": "Bureau of Fire", "authorized_total": decimal.Decimal("100000")},
        ],
        "AS ytd_spend": [
            {"department": "Public Works", "ytd_spend": decimal.Decimal("1200000")},
            {"department": "Fire", "ytd_spend": decimal.Decimal("90000")},   # variant name → merges with "Bureau of Fire"
        ],
        "FROM grants\n            WHERE end_date": [
            {"grant_name": "NEHA-FDA", "department": "Health", "end_date": datetime.date(2026, 8, 30),
             "amount": decimal.Decimal("14000")},
            {"grant_name": "Old-Grant", "department": "Admin", "end_date": datetime.date(2025, 1, 1),
             "amount": decimal.Decimal("1000")},   # already expired → excluded
        ],
    })
    out = DashboardAggregator(store, now=now)._build_commitments()

    pw = next(d for d in out["authorized_vs_spent"] if d["department"] == "Public Works")
    assert pw["authorized_total"] == 3500000.0 and pw["ytd_spend"] == 1200000.0
    fire = next(d for d in out["authorized_vs_spent"] if "Fire" in d["department"])
    assert fire["ytd_spend"] == 90000.0                    # merged via _dept_key
    assert out["authorized_vs_spent"][0]["department"] == "Public Works"   # sorted by authorized desc

    names = [g["grant_name"] for g in out["grants_expiring"]]
    assert names == ["NEHA-FDA"]                            # expired one excluded
    assert out["grants_expiring"][0]["days_left"] == 90     # 2026-06-01 → 2026-08-30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_commitments_authorized_actual_and_expiring -v`
Expected: FAIL with `AttributeError: ... '_build_commitments'`.

- [ ] **Step 3: Write the implementation**

The SQL fragments (`) AS authorized`, `AS ytd_spend`, newline+`WHERE end_date`) are chosen to be unique substrings for the fake mock:

```python
    # -- Commitments (authorized vs. actual + expiring grants) ----------------
    def _build_commitments(self, expiring_days: int = 180) -> dict:
        today = self.now.date()
        window_end = today + datetime.timedelta(days=expiring_days)
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, COALESCE(SUM(amount),0) AS authorized_total "
                        "FROM (SELECT department, amount FROM resolutions "
                        "      WHERE department IS NOT NULL AND amount IS NOT NULL) AS authorized "
                        "GROUP BY department")
            auth_rows = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT department, COALESCE(SUM(ytd_expended),0) AS ytd_spend FROM expenditures "
                        "WHERE department IS NOT NULL AND line_item NOT ILIKE '%%total%%' "
                        "AND (year, quarter) = (SELECT year, quarter FROM expenditures "
                        "WHERE year IS NOT NULL ORDER BY year DESC, quarter DESC LIMIT 1) "
                        "GROUP BY department")
            spend_rows = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT grant_name, department, end_date, amount FROM grants\n"
                        "            WHERE end_date IS NOT NULL ORDER BY end_date")
            grant_rows = [dict(r) for r in cur.fetchall()]

        # merge authorized + spend on canonical department key
        merged: dict = {}
        for r in auth_rows:
            key = self._dept_key(r["department"])
            m = merged.setdefault(key, {"names": [], "authorized_total": 0.0, "ytd_spend": 0.0})
            m["names"].append(r["department"])
            m["authorized_total"] += float(r["authorized_total"] or 0)
        for r in spend_rows:
            key = self._dept_key(r["department"])
            m = merged.setdefault(key, {"names": [], "authorized_total": 0.0, "ytd_spend": 0.0})
            m["names"].append(r["department"])
            m["ytd_spend"] += float(r["ytd_spend"] or 0)
        authorized_vs_spent = [
            {"department": max(m["names"], key=len), "authorized_total": m["authorized_total"], "ytd_spend": m["ytd_spend"]}
            for m in merged.values() if m["names"]
        ]
        authorized_vs_spent.sort(key=lambda x: -x["authorized_total"])

        grants_expiring = []
        for r in grant_rows:
            end = r["end_date"]
            if end is None or end < today or end > window_end:
                continue
            grants_expiring.append({
                "grant_name": r.get("grant_name") or "Grant",
                "department": r.get("department"),
                "end_date": end.isoformat(),
                "days_left": (end - today).days,
                "amount": float(r["amount"]) if r.get("amount") is not None else None,
            })
        grants_expiring.sort(key=lambda x: x["days_left"])
        return {"authorized_vs_spent": authorized_vs_spent, "grants_expiring": grants_expiring}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_commitments_authorized_actual_and_expiring -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): _build_commitments — authorized vs YTD spend + expiring grants"
```

---

## Task 5: Register new panels in `build()` and extend the shape test

**Files:**
- Modify: `src/dashboard/aggregator.py:396-412` (`build()`)
- Modify: `tests/dashboard/test_aggregator.py` (`test_build_never_raises_records_errors`)

**Interfaces:**
- Consumes: `_build_votes`, `_build_metrics`, `_build_vendor_spend`, `_build_commitments` (Tasks 1-4).
- Produces: `/dashboard/data` payload now carries keys `votes`, `metrics`, `vendor_spend`, `commitments`.

- [ ] **Step 1: Update `test_build_never_raises_records_errors` to expect the new panels**

```python
def test_build_never_raises_records_errors():
    out = DashboardAggregator(_BoomStore()).build()
    assert out["kpis"] is None and out["timeline"] is None and out["tables"] is None
    assert set(out["errors"].keys()) == {
        "kpis", "timeline", "tables", "departments", "resolutions",
        "goals", "legislation", "meetings", "budget", "vacancies",
        "votes", "metrics", "vendor_spend", "commitments",
    }
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py::test_build_never_raises_records_errors -v`
Expected: FAIL (errors set is missing the 4 new keys).

- [ ] **Step 3: Register the panels in `build()`**

Add the four lines after `vacancies` in the `out` dict:

```python
            "vacancies": self._safe("vacancies", self._build_vacancies, errors),
            "votes": self._safe("votes", self._build_votes, errors),
            "metrics": self._safe("metrics", self._build_metrics, errors),
            "vendor_spend": self._safe("vendor_spend", self._build_vendor_spend, errors),
            "commitments": self._safe("commitments", self._build_commitments, errors),
```

- [ ] **Step 4: Run the full aggregator suite**

Run: `python3 -m pytest tests/dashboard/test_aggregator.py -v -m "not integration"`
Expected: all pass (14 tests).

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/aggregator.py tests/dashboard/test_aggregator.py
git commit -m "feat(dashboard): serve votes, metrics, vendor_spend, commitments in dashboard payload"
```

---

## Frontend tasks (6-9)

**Verification note:** the frontend has no unit-test harness; verify by loading the running app. There is real Harrisburg data in Postgres, and these are read-only views (no LLM spend). After any `templates/redesign.html` edit, restart so Jinja re-reads the template:

```bash
bash run.sh   # kills :5001 and relaunches; wait for GET /health → {"status":"ok"}
```

Then hard-refresh the browser (Cmd+Shift+R). All new data is under the `D` global (the `/dashboard/data` payload); confirm with the browser console: `D.votes`, `D.metrics`, `D.vendor_spend`, `D.commitments`, `D.timeline`.

---

## Task 6: Frontend — quarter-over-quarter spend trend (Overview)

> **Before writing chart code, consult the `dataviz` skill.** Keep it to inline SVG (no chart library — CSP forbids external scripts). Reuse the existing palette tokens (`#16344f` navy, `pctColor()`), the `.card` container, and `tabular-nums` mono for values.

**Files:**
- Modify: `templates/redesign.html` — `renderOverview()` (~line 253) and its markup block `#v-overview` (~line 122).

**Interfaces:**
- Consumes: `D.timeline.spending` = `[{"period": "2025 Q2", "ytd_expended": 150000.0}, ...]`.

- [ ] **Step 1: Add a container to the Overview markup**

In `#v-overview`, after the KPI grid `<div id="ov-kpis" ...></div>` (line ~124), add:

```html
        <div style="display:flex;align-items:baseline;justify-content:space-between;margin:8px 0 12px"><h2 class="sec">Spending trend</h2><span style="font-size:12.5px;color:#8a867d">YTD expended by quarter</span></div>
        <div class="card" style="padding:18px 20px;margin-bottom:32px" id="ov-trend"></div>
```

- [ ] **Step 2: Render an inline-SVG bar chart in `renderOverview()`**

At the end of `renderOverview()`, append:

```javascript
  const sp = (D.timeline && D.timeline.spending) || [];
  const trend = $('#ov-trend');
  if(!sp.length){ trend.innerHTML='<div class="empty">No multi-quarter spending data yet.</div>'; }
  else {
    const max = Math.max(...sp.map(s=>s.ytd_expended||0), 1);
    const W=Math.max(sp.length*64, 240), H=150, pad=24, bw=34;
    const bars = sp.map((s,i)=>{
      const h=Math.round(((s.ytd_expended||0)/max)*(H-pad-18));
      const x=i*64+14, y=H-pad-h;
      return `<rect x="${x}" y="${y}" width="${bw}" height="${h}" rx="3" fill="#16344f"></rect>`
           + `<text x="${x+bw/2}" y="${H-pad+13}" text-anchor="middle" font-size="10" fill="#8a867d">${esc(s.period)}</text>`
           + `<text x="${x+bw/2}" y="${y-5}" text-anchor="middle" font-size="9.5" fill="#6f6b64" font-family="'JetBrains Mono',monospace">${money(s.ytd_expended)}</text>`;
    }).join('');
    trend.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="YTD expended by quarter">${bars}</svg>`;
  }
```

- [ ] **Step 3: Verify manually**

Restart (`bash run.sh`), hard-refresh, open **City at a glance**. Expected: a "Spending trend" card with one navy bar per quarter, dollar labels on top, period labels beneath. If `D.timeline.spending` is empty, an italic empty-state shows instead.

- [ ] **Step 4: Commit**

```bash
git add templates/redesign.html
git commit -m "feat(ui): quarter-over-quarter spending trend chart on overview"
```

---

## Task 7: Frontend — votes (roll-call tally in Decisions + Voting records view)

**Files:**
- Modify: `templates/redesign.html` — `renderDecisions()` (~line 272); add nav item, `#v-votes` view, `renderVotes()`, and register in `map`, `CRUMB`, and the `render()` dispatch.

**Interfaces:**
- Consumes: `D.votes.by_resolution` (keyed lookup by `resolution_number`), `D.votes.by_member`.

- [ ] **Step 1: Add the nav item**

After the Decisions nav item (line ~73, the `data-tab="decisions"` div), add:

```html
      <div class="navitem" data-tab="votes"><svg width="17" height="17" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8.5l3.5 3.5L14 4"/></svg><span>Voting records</span></div>
```

- [ ] **Step 2: Add the view container**

After `#v-decisions` closes (line ~146), add:

```html
      <!-- VOTING RECORDS -->
      <div class="view" id="v-votes">
        <div style="margin-bottom:22px"><h1 class="page">Voting records</h1><p class="psub">How each council member voted, and the roll-call on every resolution.</p></div>
        <div class="card" style="overflow:hidden;margin-bottom:30px"><table><thead><tr><th>Council member</th><th class="num">Votes</th><th class="num">Yea</th><th class="num">Nay</th><th class="num">Abstain</th><th class="num">Absent</th></tr></thead><tbody id="votes-members"></tbody></table></div>
        <h2 class="sec" style="font-size:18px;margin-bottom:12px">Roll-call by resolution</h2>
        <div id="votes-rollcall" style="display:flex;flex-direction:column;gap:12px"></div>
      </div>
```

- [ ] **Step 3: Register `votes` in the three dispatch structures**

- In `CRUMB` (line ~232) add: `votes:['Explore','Voting records'],`
- In the `map` object inside `render()` (line ~237) add: `votes:'v-votes',`
- In the render-data guard list and dispatch (the reassigned `render=function(){...}` block, ~line 445) add `'votes'` to the `dataTab` array and add `if(state.tab==='votes') renderVotes();`

- [ ] **Step 4: Add `renderVotes()`**

Add near `renderDecisions()`:

```javascript
function voteChip(n,label,cls){ return n?`<span class="${pill(cls)}" style="margin-right:4px">${n} ${label}</span>`:''; }
function renderVotes(){
  const v=D.votes||{by_member:[],by_resolution:[]};
  $('#votes-members').innerHTML = (v.by_member||[]).length
    ? v.by_member.map(m=>`<tr><td style="font-weight:500">${cell(m.member)}</td><td class="num mono">${m.total}</td><td class="num mono" style="color:#2d6a4f">${m.yea}</td><td class="num mono" style="color:#a0302a">${m.nay}</td><td class="num mono">${m.abstain}</td><td class="num mono" style="color:#8a867d">${m.absent}</td></tr>`).join('')
    : '<tr><td colspan="6" class="empty">No recorded votes yet.</td></tr>';
  $('#votes-rollcall').innerHTML = (v.by_resolution||[]).length
    ? v.by_resolution.map(r=>{const t=r.tally;return `<div class="card" style="padding:14px 18px"><div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap"><div style="min-width:0"><div style="display:flex;align-items:center;gap:8px"><span class="mono" style="font-size:12px;color:#16344f">${cell(r.resolution_number)}</span>${r.status?`<span class="${statusPill(r.status)}">${esc(r.status)}</span>`:''}${r.amount!=null?`<span class="mono" style="font-size:12px;color:#6f6b64">${money(r.amount)}</span>`:''}</div>${r.title?`<div style="font-size:13.5px;font-weight:600;margin-top:4px">${esc(r.title)}</div>`:''}</div><div style="flex-shrink:0">${voteChip(t.yea,'Yea','green')}${voteChip(t.nay,'Nay','red')}${voteChip(t.abstain,'Abstain','amber')}${voteChip(t.absent,'Absent','grey')}</div></div><div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px">${(r.votes||[]).map(x=>`<span style="font-size:12px;color:#4a4744">${esc(x.member)}: <span style="color:#6f6b64">${esc(x.vote)}</span></span>`).join(' · ')}</div></div>`;}).join('')
    : '<div class="empty">No roll-call data.</div>';
}
```

- [ ] **Step 5: Verify manually**

Restart, hard-refresh, click **Voting records**. Expected: a member table (Yea green, Nay red) and a roll-call card per resolution with colored tally chips. Empty states show if `votes` is empty.

- [ ] **Step 6: Commit**

```bash
git add templates/redesign.html
git commit -m "feat(ui): voting records view — member tallies and per-resolution roll-call"
```

---

## Task 8: Frontend — department performance metrics (Department detail)

**Files:**
- Modify: `templates/redesign.html` — `renderDeptDetail()` (~line 353).

**Interfaces:**
- Consumes: `D.metrics` = `[{"department", "metrics":[{name,value,unit,quarter,year}]}]`. Match to the current department with the existing `dkey()` helper.

- [ ] **Step 1: Build a metrics section and inject it into the detail**

In `renderDeptDetail()`, before the final `$('#dept-detail').innerHTML=...` assignment, compute:

```javascript
  const metricsEntry=(D.metrics||[]).find(x=>dkey(x.department)===k);
  const metricCards=(metricsEntry&&metricsEntry.metrics.length)
    ? `<div style="display:flex;align-items:baseline;justify-content:space-between;margin:30px 0 14px"><h2 class="sec">Performance metrics</h2><span style="font-size:12.5px;color:#8a867d">latest reported</span></div>
       <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px">${metricsEntry.metrics.map(m=>`<div class="card" style="padding:14px 16px"><div class="mono" style="font-size:20px;color:#16344f">${m.value==null?'—':m.value.toLocaleString()}${m.unit?` <span style=\"font-size:12px;color:#a7a298\">${esc(m.unit)}</span>`:''}</div><div style="font-size:12px;color:#4a4744;margin-top:6px">${esc(m.name)}</div><div style="font-size:10.5px;color:#a7a298;margin-top:3px">${esc(m.quarter||'')} ${m.year||''}</div></div>`).join('')}</div>`
    : '';
```

Then append `${metricCards}` into the detail template literal, immediately after the `${vacCard}` interpolation.

- [ ] **Step 2: Verify manually**

Restart, hard-refresh, open **Departments** → a department that reports metrics (e.g. Fire). Expected: a "Performance metrics" grid of metric cards (value + unit + period). Departments with no metrics show nothing extra (no empty box).

- [ ] **Step 3: Commit**

```bash
git add templates/redesign.html
git commit -m "feat(ui): surface department performance metrics on department detail"
```

---

## Task 9: Frontend — Follow-through view (authorized vs. spent + expiring grants + vendor spend)

**Files:**
- Modify: `templates/redesign.html` — add nav item, `#v-followthrough` view, `renderFollowthrough()`, and register in `map`, `CRUMB`, dispatch.

**Interfaces:**
- Consumes: `D.commitments.authorized_vs_spent`, `D.commitments.grants_expiring`, `D.vendor_spend`.

- [ ] **Step 1: Add the nav item** (after the Money nav item, line ~74)

```html
      <div class="navitem" data-tab="followthrough"><svg width="17" height="17" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8.5l3.5 3.5L14 4"/><path d="M2 3.5h8"/></svg><span>Follow-through</span></div>
```

- [ ] **Step 2: Add the view container** (after `#v-money` closes, ~line 164)

```html
      <!-- FOLLOW-THROUGH -->
      <div class="view" id="v-followthrough">
        <div style="margin-bottom:22px"><h1 class="page">Follow-through</h1><p class="psub">What the council authorized vs. what departments have spent, grants nearing expiration, and total spend by vendor.</p></div>
        <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:12px"><h2 class="sec">Authorized vs. spent (directional)</h2></div>
        <div class="card" style="overflow:hidden;margin-bottom:14px"><table><thead><tr><th>Department</th><th class="num">Council-authorized</th><th class="num">YTD spent</th></tr></thead><tbody id="ft-auth"></tbody></table></div>
        <p style="font-size:11.5px;color:#a7a298;margin-bottom:30px">Authorized = all council resolution $ for the department. YTD spent = latest-period expenditures. Shown side by side as context — not a line-item reconciliation.</p>
        <h2 class="sec" style="font-size:18px;margin-bottom:12px">Grants expiring soon</h2>
        <div class="card" style="overflow:hidden;margin-bottom:30px"><table><thead><tr><th>Grant</th><th>Department</th><th class="num">Days left</th><th>End date</th><th class="num">Award</th></tr></thead><tbody id="ft-grants"></tbody></table></div>
        <h2 class="sec" style="font-size:18px;margin-bottom:12px">Spend by vendor</h2>
        <div class="card" style="overflow:hidden"><table><thead><tr><th>Vendor</th><th class="num">Resolutions</th><th class="num">Total</th></tr></thead><tbody id="ft-vendors"></tbody></table></div>
      </div>
```

- [ ] **Step 3: Register `followthrough`** — add `followthrough:['Explore','Follow-through'],` to `CRUMB`; `followthrough:'v-followthrough',` to `map`; `'followthrough'` to the `dataTab` array and `if(state.tab==='followthrough') renderFollowthrough();` to the dispatch.

- [ ] **Step 4: Add `renderFollowthrough()`**

```javascript
function renderFollowthrough(){
  const c=D.commitments||{authorized_vs_spent:[],grants_expiring:[]};
  $('#ft-auth').innerHTML=(c.authorized_vs_spent||[]).length
    ? c.authorized_vs_spent.map(d=>`<tr><td style="font-weight:500">${cell(d.department)}</td><td class="num mono">${money(d.authorized_total)}</td><td class="num mono" style="color:#6f6b64">${money(d.ytd_spend)}</td></tr>`).join('')
    : '<tr><td colspan="3" class="empty">No authorized spend recorded.</td></tr>';
  $('#ft-grants').innerHTML=(c.grants_expiring||[]).length
    ? c.grants_expiring.map(g=>`<tr><td style="font-weight:500">${cell(g.grant_name)}</td><td style="color:#6f6b64">${cell(g.department)}</td><td class="num mono" style="color:${g.days_left<=60?'#a0302a':g.days_left<=120?'#c4691e':'#16344f'}">${g.days_left}</td><td class="mono" style="font-size:12px;color:#6f6b64">${cell(g.end_date)}</td><td class="num mono">${money(g.amount)}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No grants expiring in the next 180 days.</td></tr>';
  const vs=D.vendor_spend||[];
  $('#ft-vendors').innerHTML=vs.length
    ? vs.map(v=>`<tr><td style="font-weight:500">${cell(v.vendor)}</td><td class="num mono">${v.count}</td><td class="num mono">${money(v.total)}</td></tr>`).join('')
    : '<tr><td colspan="3" class="empty">No vendor spend recorded.</td></tr>';
}
```

- [ ] **Step 5: Verify manually**

Restart, hard-refresh, click **Follow-through**. Expected: three tables — authorized vs. spent per department, expiring grants (days-left colored red/amber/navy), and vendor totals sorted high→low. Empty states render where data is missing.

- [ ] **Step 6: Commit**

```bash
git add templates/redesign.html
git commit -m "feat(ui): follow-through view — authorized vs spent, expiring grants, vendor spend"
```

---

## Self-Review

**Spec coverage:**
- Tier 1 · votes → Tasks 1, 7 ✅
- Tier 1 · metrics → Tasks 2, 8 ✅
- Tier 1 · quarter trends (wire `_build_timeline` + charts) → Tasks 0 (data), 6 (chart) ✅
- Tier 1 · vendor spend → Tasks 3, 9 ✅
- Commitment v1 · authorized vs actual (dept-level) → Tasks 4, 9 ✅
- Commitment v1 · grant-expiration → Tasks 4, 9 ✅
- Commitment v1 · goal follow-through → reused via existing `goals` panel (noted in Task 4 interface; no new work) ✅
- Deferred (v2): per-resolution fuzzy expenditure matching — explicitly out of scope ✅
- Constraint · no LLM/ingest spend → all tasks SQL+UI only ✅

**Placeholder scan:** every code step contains complete code; no TBD/TODO/"handle edge cases". ✅

**Type consistency:** frontend consumers match backend shapes — `D.votes.{by_member,by_resolution}` (Task 1↔7), `D.metrics[].metrics[]` (Task 2↔8), `D.vendor_spend[].{vendor,count,total}` (Task 3↔9), `D.commitments.{authorized_vs_spent,grants_expiring}` (Task 4↔9), `D.timeline.spending[].{period,ytd_expended}` (Task 0↔6). ✅

**Known risk to watch during execution:** the `_FakeCursor` matches SQL by *substring*, so a new query whose text contains an existing mock key will grab the wrong canned rows. Each new method's test uses a deliberately unique fragment (`) AS authorized`, `AS ytd_spend`, `WHERE vendor`, newline+`WHERE end_date`). If you edit a builder's SQL, re-check its test key still uniquely matches.
