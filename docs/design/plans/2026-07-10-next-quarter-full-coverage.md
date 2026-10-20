# Next-Quarter Full-Coverage Question Generation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the "Next quarter" tab generate prioritized, data-grounded questions for essentially every department by broadening deterministic detection from 3 gap signals to 5 signal families, and upgrade the on-demand per-department LLM pass from polish-only to polish + cross-table synthesis.

**Architecture:** Keep the existing free/paid split intact. All *detection* stays 100% deterministic SQL in `src/dashboard/review_questions.py`, called from the always-on `DashboardAggregator.build()` (zero LLM cost). The single optional LLM call remains gated behind the `/questions/<dept>` route (one call per department, cached by findings-hash), now also emitting up to 3 synthesis questions. Frontend renders all departments, ranked by priority, with a soft cap.

**Tech Stack:** Python 3, Flask, psycopg (dict cursors), pytest with a substring-matching `_FakeCursor`/`_FakeStore` mock (no DB, no real LLM), vanilla JS in a Jinja template.

## Global Constraints

- **No LLM spend in the always-on path.** `ReviewQuestions.build()` and every signal method are deterministic SQL only. The LLM (`phrase_questions`) is called *only* from the `/questions/<dept>` route, on demand, cached. Verbatim from spec: "The LLM pass must never run inside the always-on dashboard build."
- **All tests mock the Anthropic client** (`_FakeLLM` / monkeypatched `phrase_questions`). No test performs real LLM or DB I/O.
- **Canonical department keying is mandatory.** Every signal method groups by `DashboardAggregator._dept_key(name)` and sets the finding's display name via `DashboardAggregator._dept_display(key, names)`. Name variants (e.g. "Parks & Recreation" vs "Bureau of Parks & Recreation") must merge into one department.
- **Every finding dict has exactly these keys:** `signal`, `department`, `question`, `priority`, `evidence`. `priority` ∈ {`"highest"`, `"high"`, `"medium"`, `"low"`}.
- **Preserve facts in phrasing.** The LLM may only rephrase; it must not invent, drop, or alter any number, percentage, target, grant name, or department name.
- Run tests with: `python -m pytest tests/dashboard/ -v` from the repo root `/Users/leenadudi/council.knowledge.base`.

---

## File Structure

- **Modify** `src/dashboard/review_questions.py` — add module constants (`_PRIORITY`, status word lists, caps); rewrite `_goal_findings` to emit 4 goal signals with priority; add `_vacancy_findings`, `_grant_findings`, `_quiet_department_findings`; add `priority` to `_budget_findings`; rewrite `build()` to merge all 5 families and sort by priority; rewrite `phrase_questions` to take findings and return `{polished, synthesis}`.
- **Modify** `app.py:233-269` — `/questions/<dept>` route to consume the new `phrase_questions` return shape and emit `priority` + `synthesis`.
- **Modify** `templates/redesign.html` — `Q_SIG` map, header/empty-state copy, `renderQuestions()` (all-department picker, priority order, soft cap + "show all", synthesis rendering), `applyPolish()`.
- **Modify** `tests/dashboard/test_review_questions.py` — update the 3 behavior-change tests; add tests for the 4 new/updated signals, priority ordering, and the new `phrase_questions` shape.
- **Modify** `tests/dashboard/test_questions_route.py` — update fakes to the `{polished, synthesis}` shape; add a synthesis assertion.

---

## Task 1: Goals — all statuses, with priority (+ build() priority sort)

Rewrite `_goal_findings` so goals of *every* status generate a forward-looking question (not only gaps), tag each finding with `priority`, and make `build()` sort each department's findings by priority. This is the main coverage driver and contains the approved **demote-not-suppress** behavior change.

**Files:**
- Modify: `src/dashboard/review_questions.py` (module constants; `_goal_findings`; `build()`)
- Test: `tests/dashboard/test_review_questions.py`

**Interfaces:**
- Consumes: `DashboardAggregator._dept_key`, `._dept_display` (already imported as `_dept_key`, `_dept_display`).
- Produces:
  - `_goal_findings(self) -> tuple[dict, dict]` returning `(findings_by_key, meta)` where `findings_by_key: dict[str, list[dict]]` keyed by canonical dept key, and `meta = {"period": <label|"">}`. (Note: the old 3-tuple return is replaced by a 2-tuple — `names_by_key` is no longer returned.)
  - Each finding: `{signal, department, question, priority, evidence}`.
  - Goal signals + priority: `goal_stalled`=`highest`, `goal_no_progress`=`high`, `goal_in_progress`=`medium`, `goal_completed`=`low`.
  - `build(self) -> dict` unchanged shape `{"period", "departments":[{department, findings}]}`, findings sorted by `(_PRIORITY[priority], signal)`.

- [ ] **Step 1: Update the 3 existing behavior-change tests to the new demote-not-suppress behavior**

In `tests/dashboard/test_review_questions.py`, replace the three tests below wholesale.

`test_goal_with_changing_status_is_not_stalled` — a goal reporting active progress is no longer *stalled*, but now produces a `goal_in_progress` follow-up (it did produce nothing before):

```python
def test_goal_with_reported_status_is_in_progress_not_stalled():
    store = _FakeStore({
        GOALS: [
            _goal(1, "Bureau of Fire", 2025, "Q3", "Hire 3 EMTs", target="3 hires", status="1 of 3 hired"),
            _goal(2, "Bureau of Fire", 2025, "Q4", "Hire 3 EMTs", target="3 hires", status="2 of 3 hired"),
        ],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    # progress is being reported -> not stalled; a forward-looking follow-up instead
    assert [f["signal"] for f in d["findings"]] == ["goal_in_progress"]
    assert d["findings"][0]["priority"] == "medium"
    assert "2 of 3 hired" in d["findings"][0]["question"]
```

`test_clerk_user_status_suppresses_no_progress` — a clerk-set status now *demotes* to a forward-looking question rather than removing it:

```python
def test_clerk_user_status_demotes_no_progress_to_followup():
    store = _FakeStore({
        GOALS: [_goal(1, "Bureau of Fire", 2026, "Q1", "Reduce response time",
                      target="< 6 min", status=None, user_status="in_progress")],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    assert [f["signal"] for f in d["findings"]] == ["goal_in_progress"]
    assert d["findings"][0]["priority"] == "medium"
```

`test_clerk_user_status_suppresses_stalled` — clerk-marked-completed latest now yields a `goal_completed` follow-on:

```python
def test_clerk_completed_status_yields_followon_question():
    store = _FakeStore({
        GOALS: [
            _goal(1, "Bureau of Fire", 2025, "Q3", "Hire 3 EMTs", target="3", status=None),
            _goal(2, "Bureau of Fire", 2025, "Q4", "Hire 3 EMTs", target="3", status=None,
                  user_status="completed"),
        ],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    assert [f["signal"] for f in d["findings"]] == ["goal_completed"]
    assert d["findings"][0]["priority"] == "low"
    assert "follow-on" in d["findings"][0]["question"]
```

- [ ] **Step 2: Add tests for the new goal branches and priority ordering**

Append to `tests/dashboard/test_review_questions.py`:

```python
def test_in_progress_and_no_progress_sort_by_priority():
    # same dept: one no-status goal (high) and one in-progress goal (medium);
    # build() must order high before medium.
    store = _FakeStore({
        GOALS: [
            _goal(1, "Public Works", 2026, "Q2", "Pave 10 roads", target="10", status=None),
            _goal(2, "Public Works", 2026, "Q2", "Replace signals", target="5", status="3 replaced"),
        ],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    assert [f["signal"] for f in d["findings"]] == ["goal_no_progress", "goal_in_progress"]

def test_old_goal_not_in_latest_period_is_skipped():
    # a lone goal from an older period (not repeated) is not asked about.
    store = _FakeStore({
        GOALS: [
            _goal(1, "Bureau of Fire", 2025, "Q1", "Old goal", status="done"),
            _goal(2, "Bureau of Fire", 2026, "Q1", "New goal", status=None),
        ],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    # only the latest-period goal surfaces
    assert [f["signal"] for f in d["findings"]] == ["goal_no_progress"]
    assert "New goal" in d["findings"][0]["question"]
```

- [ ] **Step 3: Run the goal tests to verify they fail**

Run: `python -m pytest tests/dashboard/test_review_questions.py -k "in_progress or demotes or completed_status or sort_by_priority or not_in_latest or reported_status" -v`
Expected: FAIL (old code returns the old 3-tuple / suppresses / has no `priority` key / signal `goal_in_progress` does not exist).

- [ ] **Step 4: Add module constants and rewrite `_goal_findings` + `build()`**

In `src/dashboard/review_questions.py`, add constants just below `_PACE_BEHIND` (line 22):

```python
# Priority ordering for ranking findings within a department (lower = shown first).
_PRIORITY = {"highest": 0, "high": 1, "medium": 2, "low": 3}
# A goal status containing any of these words is treated as completed.
_STATUS_COMPLETE = ("complete", "done", "achieved", "finished", "closed", "met")
```

Add a helper near `_norm_title` (after line 29):

```python
def _classify_status(s: str) -> str:
    """none (no status), completed, or in_progress (any other non-empty status)."""
    s = (s or "").strip().lower()
    if not s:
        return "none"
    if any(w in s for w in _STATUS_COMPLETE):
        return "completed"
    return "in_progress"
```

Replace the entire `_goal_findings` method (lines 47-133) with:

```python
    # -- signal: goal follow-ups (all statuses) -------------------------------
    def _goal_findings(self):
        """Returns (findings_by_key, meta).

        Every goal produces a forward-looking question. Priority ranks gaps above
        routine follow-ups: stalled > no-progress > in-progress > completed. A
        clerk-set user_status demotes a goal to an in-progress/completed follow-up
        rather than removing it (it still prompts a next-quarter question).
        """
        with self.sql.cursor() as cur:
            cur.execute("SELECT id, department, year, quarter, goal_title, description, "
                        "target, status, user_status FROM goals ORDER BY department, id")
            rows = [dict(r) for r in cur.fetchall()]

        def eff(r):
            return (r.get("user_status") or r.get("status") or "").strip()

        names_by_key: dict = {}
        for r in rows:
            names_by_key.setdefault(_dept_key(r["department"]), set()).add(r["department"])

        periods = {_period_tuple(r.get("year"), r.get("quarter")) for r in rows}
        latest = max(periods) if periods else (0, "")

        history: dict = {}
        for r in rows:
            k = (_dept_key(r["department"]), _norm_title(r["goal_title"]))
            history.setdefault(k, []).append(r)

        findings: dict = {}   # dept_key -> [finding]
        for (dkey, ntitle), hist in history.items():
            if not dkey or not ntitle:
                continue
            hist.sort(key=lambda r: _period_tuple(r.get("year"), r.get("quarter")))
            distinct_periods = sorted({_period_tuple(r.get("year"), r.get("quarter")) for r in hist})
            display = _dept_display(dkey, names_by_key[dkey])
            latest_row = hist[-1]
            title = latest_row.get("goal_title") or ntitle
            e = eff(latest_row)
            cls = _classify_status(e)
            in_latest = _period_tuple(latest_row.get("year"), latest_row.get("quarter")) == latest
            tgt = str(latest_row.get("target") or "").strip()
            tgt_clause = f" (target: {tgt})" if tgt else ""

            # stalled: same title across >=2 periods, no status anywhere -> sharpest gap
            if len(distinct_periods) >= 2 and all(not eff(r) for r in hist):
                first_lbl = _period_label(*distinct_periods[0])
                last_lbl = _period_label(*distinct_periods[-1])
                findings.setdefault(dkey, []).append({
                    "signal": "goal_stalled", "department": display, "priority": "highest",
                    "question": (f"“{title}” has appeared in {len(distinct_periods)} quarterly "
                                 f"reports ({first_lbl}→{last_lbl}) with no status update — what "
                                 f"progress has been made since {last_lbl}?"),
                    "evidence": {"goal_title": title, "periods": [_period_label(*p) for p in distinct_periods],
                                 "count": len(distinct_periods)},
                })
                continue

            # only ask about goals whose latest appearance is in the latest period
            if not in_latest:
                continue

            if cls == "none":
                findings.setdefault(dkey, []).append({
                    "signal": "goal_no_progress", "department": display, "priority": "high",
                    "question": (f"{display}'s goal “{title}”{tgt_clause} has no progress "
                                 f"reported for {_period_label(*latest)}. What's the current status?"),
                    "evidence": {"goal_title": title, "target": tgt or None,
                                 "year": latest_row.get("year"), "quarter": latest_row.get("quarter")},
                })
            elif cls == "completed":
                findings.setdefault(dkey, []).append({
                    "signal": "goal_completed", "department": display, "priority": "low",
                    "question": (f"{display} reported goal “{title}” as complete (“{e}”). "
                                 f"What's the follow-on objective for next quarter?"),
                    "evidence": {"goal_title": title, "status": e,
                                 "year": latest_row.get("year"), "quarter": latest_row.get("quarter")},
                })
            else:  # in_progress
                findings.setdefault(dkey, []).append({
                    "signal": "goal_in_progress", "department": display, "priority": "medium",
                    "question": (f"{display}'s goal “{title}”{tgt_clause} was reported “{e}” as of "
                                 f"{_period_label(*latest)}. What progress has been made since?"),
                    "evidence": {"goal_title": title, "target": tgt or None, "status": e,
                                 "year": latest_row.get("year"), "quarter": latest_row.get("quarter")},
                })

        return findings, {"period": _period_label(*latest) if latest[0] else ""}
```

Replace `build()` (lines 192-207) with (budget/vacancy/grant/quiet wired in later tasks — for now merge goals + budget and sort by priority):

```python
    def build(self) -> dict:
        goals, meta = self._goal_findings()
        budget = self._budget_findings()

        by_key: dict = {}
        for src in (goals, budget):
            for k, v in src.items():
                by_key.setdefault(k, []).extend(v)

        departments = []
        for findings in by_key.values():
            if not findings:
                continue
            findings.sort(key=lambda f: (_PRIORITY.get(f.get("priority", "medium"), 9), f["signal"]))
            departments.append({"department": findings[0]["department"], "findings": findings})
        departments.sort(key=lambda d: d["department"].lower())
        return {"period": meta.get("period") or None, "departments": departments}
```

- [ ] **Step 5: Add `priority` to the two budget findings**

In `_budget_findings` (lines 181-188), add `"priority": "high",` to the finding dict (both branches share one dict append):

```python
            findings.setdefault(k, []).append({
                "signal": "budget_pace", "department": display, "priority": "high",
                "question": q,
                "evidence": {"revised_budget": v["rb"], "ytd_expended": v["ytd"],
                             "pace": round(pace, 3), "expected": expected,
                             "direction": "ahead" if ahead else "behind"},
            })
```

- [ ] **Step 6: Run all review-questions tests to verify goal + budget tests pass**

Run: `python -m pytest tests/dashboard/test_review_questions.py -v`
Expected: PASS for all goal/budget/variant/phrasing tests. (The `phrase_questions` tests still pass — that function is untouched until Task 5.)

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/review_questions.py tests/dashboard/test_review_questions.py
git commit -m "feat(next-quarter): goals of all statuses generate ranked follow-ups

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Vacancy signal

Add `_vacancy_findings`: for each department, the latest period's open positions become one hiring-status question.

**Files:**
- Modify: `src/dashboard/review_questions.py` (add `_vacancy_findings`; wire into `build()`)
- Test: `tests/dashboard/test_review_questions.py`

**Interfaces:**
- Produces: `_vacancy_findings(self) -> dict[str, list[dict]]` keyed by canonical dept key. Signal `vacancy`, priority `medium`. Evidence `{period, positions:[{title, count}], total_open}`.

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_review_questions.py`:

```python
VAC = "FROM vacancies"

def test_vacancy_signal_aggregates_open_positions():
    store = _FakeStore({
        GOALS: [], PERIOD: [], BUDGET: [],
        VAC: [
            {"department": "Bureau of Police", "position_title": "Patrol Officer", "open_count": 25,
             "quarter": "Q1", "year": 2026},
            {"department": "Bureau of Police", "position_title": "Supervisor", "open_count": 4,
             "quarter": "Q1", "year": 2026},
        ],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    f = d["findings"][0]
    assert f["signal"] == "vacancy" and f["priority"] == "medium"
    assert f["evidence"]["total_open"] == 29
    assert "Patrol Officer" in f["question"] and "hiring status" in f["question"]

def test_vacancy_signal_uses_only_latest_period():
    store = _FakeStore({
        GOALS: [], PERIOD: [], BUDGET: [],
        VAC: [
            {"department": "Bureau of Police", "position_title": "Patrol Officer", "open_count": 25,
             "quarter": "Q1", "year": 2025},
            {"department": "Bureau of Police", "position_title": "Detective", "open_count": 2,
             "quarter": "Q1", "year": 2026},
        ],
    })
    f = ReviewQuestions(store).build()["departments"][0]["findings"][0]
    # only the 2026 opening counts; the 2025 row is stale
    assert f["evidence"]["total_open"] == 2 and "Detective" in f["question"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/dashboard/test_review_questions.py -k vacancy -v`
Expected: FAIL (no vacancy findings; departments empty).

- [ ] **Step 3: Implement `_vacancy_findings` and wire into `build()`**

Add the method after `_budget_findings` in `src/dashboard/review_questions.py`:

```python
    # -- signal: open vacancies -----------------------------------------------
    def _vacancy_findings(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, position_title, open_count, quarter, year "
                        "FROM vacancies WHERE LOWER(COALESCE(status,'')) = 'open' "
                        "AND department IS NOT NULL AND position_title IS NOT NULL "
                        "AND position_title <> '' AND LOWER(position_title) <> 'none'")
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {}

        by_key: dict = {}
        for r in rows:
            k = _dept_key(r["department"])
            acc = by_key.setdefault(k, {"names": set(), "latest": (0, ""), "rows": {}})
            acc["names"].add(r["department"])
            p = _period_tuple(r.get("year"), r.get("quarter"))
            if p > acc["latest"]:
                acc["latest"] = p
            acc["rows"].setdefault(p, []).append(r)

        findings: dict = {}
        for k, acc in by_key.items():
            if not k:
                continue
            latest_rows = acc["rows"].get(acc["latest"], [])
            positions = [{"title": r["position_title"].strip(),
                          "count": int(r["open_count"]) if r.get("open_count") is not None else None}
                         for r in latest_rows]
            if not positions:
                continue
            total = sum(p["count"] for p in positions if p["count"]) or None
            display = _dept_display(k, acc["names"])
            plbl = _period_label(*acc["latest"])
            listing = ", ".join(f"{p['title']}" + (f" ({p['count']})" if p["count"] else "")
                                for p in positions)
            count_clause = f"{total} open position{'s' if total != 1 else ''}" if total else \
                           f"{len(positions)} open role{'s' if len(positions) != 1 else ''}"
            findings.setdefault(k, []).append({
                "signal": "vacancy", "department": display, "priority": "medium",
                "question": (f"{display} reported {count_clause} in {plbl} ({listing}). "
                             f"What's the current hiring status?"),
                "evidence": {"period": plbl, "positions": positions, "total_open": total},
            })
        return findings
```

In `build()`, add `vac = self._vacancy_findings()` after the budget line and include it in the merge tuple:

```python
        goals, meta = self._goal_findings()
        budget = self._budget_findings()
        vac = self._vacancy_findings()

        by_key: dict = {}
        for src in (goals, budget, vac):
            for k, v in src.items():
                by_key.setdefault(k, []).extend(v)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/dashboard/test_review_questions.py -v`
Expected: PASS (all, including the new vacancy tests; existing goal/budget tests unaffected because they supply no `VAC` key → empty).

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/review_questions.py tests/dashboard/test_review_questions.py
git commit -m "feat(next-quarter): vacancy signal for open positions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Grant signal

Add `_grant_findings`: active/recent grants (excluding clearly-closed) become drawdown questions, capped per department.

**Files:**
- Modify: `src/dashboard/review_questions.py` (add `_grant_findings`; wire into `build()`; add `_GRANTS_PER_DEPT` constant)
- Test: `tests/dashboard/test_review_questions.py`

**Interfaces:**
- Produces: `_grant_findings(self) -> dict[str, list[dict]]`. Signal `grant`, priority `low`. Evidence `{grant_name, grant_number, amount, status, end_date}`. At most `_GRANTS_PER_DEPT` (5) findings per department, highest amount first.

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_review_questions.py`:

```python
GRANTS = "FROM grants"

def test_grant_signal_flags_active_and_skips_closed():
    store = _FakeStore({
        GOALS: [], PERIOD: [], BUDGET: [],
        GRANTS: [
            {"department": "Bureau of Fire", "grant_name": "SAFER Grant", "grant_number": "S-1",
             "amount": 500000.0, "end_date": None, "status": "active"},
            {"department": "Bureau of Fire", "grant_name": "Old Grant", "grant_number": "O-1",
             "amount": 10000.0, "end_date": None, "status": "closed"},
        ],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    sigs = [f["signal"] for f in d["findings"]]
    assert sigs == ["grant"]                       # closed grant excluded
    assert "SAFER Grant" in d["findings"][0]["question"]
    assert d["findings"][0]["priority"] == "low"

def test_grant_signal_caps_per_department():
    store = _FakeStore({
        GOALS: [], PERIOD: [], BUDGET: [],
        GRANTS: [
            {"department": "Public Works", "grant_name": f"Grant {i}", "grant_number": str(i),
             "amount": float(i), "end_date": None, "status": "active"}
            for i in range(1, 9)   # 8 active grants
        ],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    assert len(d["findings"]) == 5                 # capped at _GRANTS_PER_DEPT
    # highest amount first
    assert "Grant 8" in d["findings"][0]["question"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/dashboard/test_review_questions.py -k grant -v`
Expected: FAIL.

- [ ] **Step 3: Implement `_grant_findings` and wire into `build()`**

Add constant next to `_STATUS_COMPLETE`:

```python
_GRANTS_PER_DEPT = 5
_GRANT_CLOSED = ("closed", "complete", "expired", "terminated", "ended")
```

Add the method after `_vacancy_findings`:

```python
    # -- signal: active grants ------------------------------------------------
    def _grant_findings(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, grant_name, grant_number, amount, end_date, status "
                        "FROM grants WHERE department IS NOT NULL AND grant_name IS NOT NULL "
                        "AND grant_name <> ''")
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {}

        by_key: dict = {}
        for r in rows:
            status = (r.get("status") or "").strip().lower()
            if any(w in status for w in _GRANT_CLOSED):
                continue
            k = _dept_key(r["department"])
            by_key.setdefault(k, {"names": set(), "rows": []})
            by_key[k]["names"].add(r["department"])
            by_key[k]["rows"].append(r)

        findings: dict = {}
        for k, acc in by_key.items():
            if not k:
                continue
            display = _dept_display(k, acc["names"])
            ranked = sorted(acc["rows"], key=lambda r: float(r.get("amount") or 0), reverse=True)
            for r in ranked[:_GRANTS_PER_DEPT]:
                amt = r.get("amount")
                amt_clause = f" (${float(amt):,.0f})" if amt is not None else ""
                status_word = (r.get("status") or "active").strip() or "active"
                end = r.get("end_date")
                findings.setdefault(k, []).append({
                    "signal": "grant", "department": display, "priority": "low",
                    "question": (f"{display}'s grant “{r['grant_name']}”{amt_clause} was "
                                 f"reported {status_word}. What's the current status / drawdown "
                                 f"for next quarter?"),
                    "evidence": {"grant_name": r["grant_name"], "grant_number": r.get("grant_number"),
                                 "amount": float(amt) if amt is not None else None,
                                 "status": r.get("status"),
                                 "end_date": end.isoformat() if hasattr(end, "isoformat") else end},
                })
        return findings
```

In `build()`, add `grant = self._grant_findings()` and include `grant` in the merge tuple:

```python
        vac = self._vacancy_findings()
        grant = self._grant_findings()

        by_key: dict = {}
        for src in (goals, budget, vac, grant):
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/dashboard/test_review_questions.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/review_questions.py tests/dashboard/test_review_questions.py
git commit -m "feat(next-quarter): active-grant drawdown signal (capped per dept)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Quiet-department signal

Add `_quiet_department_findings`: any department whose latest filed quarterly report predates the overall latest filing period gets an "hasn't filed since…" prompt. This guarantees behind-filing departments surface.

**Files:**
- Modify: `src/dashboard/review_questions.py` (add `_quiet_department_findings`; wire into `build()`)
- Test: `tests/dashboard/test_review_questions.py`

**Interfaces:**
- Produces: `_quiet_department_findings(self) -> dict[str, list[dict]]`. Signal `quiet_department`, priority `high`. Evidence `{last_filed_period, latest_period}`.

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_review_questions.py`:

```python
DOCS = "FROM documents"

def test_quiet_department_flagged_when_behind():
    store = _FakeStore({
        GOALS: [], PERIOD: [], BUDGET: [],
        DOCS: [
            {"department": "Bureau of Fire", "quarter": "Q2", "year": 2026},   # current
            {"department": "City Planning", "quarter": "Q4", "year": 2025},    # behind
        ],
    })
    depts = ReviewQuestions(store).build()["departments"]
    quiet = [d for d in depts if d["findings"][0]["signal"] == "quiet_department"]
    assert len(quiet) == 1 and quiet[0]["department"] == "City Planning"
    f = quiet[0]["findings"][0]
    assert f["priority"] == "high" and "Q4 2025" in f["question"]

def test_no_quiet_department_when_all_current():
    store = _FakeStore({
        GOALS: [], PERIOD: [], BUDGET: [],
        DOCS: [
            {"department": "Bureau of Fire", "quarter": "Q2", "year": 2026},
            {"department": "Public Works", "quarter": "Q2", "year": 2026},
        ],
    })
    assert ReviewQuestions(store).build()["departments"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/dashboard/test_review_questions.py -k quiet -v`
Expected: FAIL.

- [ ] **Step 3: Implement `_quiet_department_findings` and wire into `build()`**

Add the method after `_grant_findings`:

```python
    # -- signal: department behind on filing ----------------------------------
    def _quiet_department_findings(self):
        with self.sql.cursor() as cur:
            cur.execute("SELECT department, quarter, year FROM documents "
                        "WHERE document_type = 'quarterly_report' "
                        "AND department IS NOT NULL AND department <> ''")
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {}

        by_key: dict = {}
        for r in rows:
            k = _dept_key(r["department"])
            if not k:
                continue
            acc = by_key.setdefault(k, {"names": set(), "latest": (0, "")})
            acc["names"].add(r["department"])
            p = _period_tuple(r.get("year"), r.get("quarter"))
            if p > acc["latest"]:
                acc["latest"] = p

        overall = max((acc["latest"] for acc in by_key.values()), default=(0, ""))
        if not overall[0]:
            return {}

        findings: dict = {}
        for k, acc in by_key.items():
            if acc["latest"] >= overall:
                continue
            display = _dept_display(k, acc["names"])
            last_lbl = _period_label(*acc["latest"])
            overall_lbl = _period_label(*overall)
            since = last_lbl or "the period on record"
            findings.setdefault(k, []).append({
                "signal": "quiet_department", "department": display, "priority": "high",
                "question": (f"{display} hasn't filed a quarterly report since {since} "
                             f"(latest on record is {overall_lbl}). Please provide an update."),
                "evidence": {"last_filed_period": last_lbl or None, "latest_period": overall_lbl},
            })
        return findings
```

In `build()`, add `quiet = self._quiet_department_findings()` and include `quiet` in the merge tuple:

```python
        grant = self._grant_findings()
        quiet = self._quiet_department_findings()

        by_key: dict = {}
        for src in (goals, budget, vac, grant, quiet):
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/dashboard/test_review_questions.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/review_questions.py tests/dashboard/test_review_questions.py
git commit -m "feat(next-quarter): quiet-department signal for behind-filing depts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: LLM pass — polish + synthesis; route wiring

Upgrade `phrase_questions` to take the findings list and return `{polished, synthesis}` (polish preserves facts; synthesis adds up to 3 cross-cutting questions). Update `/questions/<dept>` and its tests.

**Files:**
- Modify: `src/dashboard/review_questions.py` (`_PHRASE_SYSTEM`, `phrase_questions`)
- Modify: `app.py:233-269` (`/questions/<dept>`)
- Test: `tests/dashboard/test_review_questions.py`, `tests/dashboard/test_questions_route.py`

**Interfaces:**
- Produces: `phrase_questions(findings, settings, client=None) -> dict` where `findings` is the list of finding dicts. Returns `{"polished": [<len==len(findings)>], "synthesis": [<0..3 str>]}`. Raises `ValueError` on polished-length mismatch.
- Route response JSON: `{department, questions:[{question, signal, priority, evidence}], synthesis:[str], polished:bool}`.

- [ ] **Step 1: Update the phrasing unit tests to the new shape**

In `tests/dashboard/test_review_questions.py`, replace the three `phrase_questions` tests (currently passing plain string lists) with:

```python
def _finding(q):
    return {"question": q, "signal": "goal_no_progress", "priority": "high", "evidence": {}}

def test_phrase_questions_returns_polished_and_synthesis():
    llm = _FakeLLM('{"polished": ["Q1 polished", "Q2 polished"], "synthesis": ["Cross-cut?"]}')
    out = phrase_questions([_finding("Q1"), _finding("Q2")], _Settings(), client=llm)
    assert out["polished"] == ["Q1 polished", "Q2 polished"]
    assert out["synthesis"] == ["Cross-cut?"]
    assert llm.calls == 1

def test_phrase_questions_empty_makes_no_call():
    llm = _FakeLLM("{}")
    out = phrase_questions([], _Settings(), client=llm)
    assert out == {"polished": [], "synthesis": []} and llm.calls == 0

def test_phrase_questions_polished_count_mismatch_raises():
    llm = _FakeLLM('{"polished": ["only one"], "synthesis": []}')
    with pytest.raises(ValueError):
        phrase_questions([_finding("Q1"), _finding("Q2")], _Settings(), client=llm)

def test_phrase_questions_caps_synthesis_at_three():
    llm = _FakeLLM('{"polished": ["p"], "synthesis": ["a", "b", "c", "d"]}')
    out = phrase_questions([_finding("Q1")], _Settings(), client=llm)
    assert out["synthesis"] == ["a", "b", "c"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/dashboard/test_review_questions.py -k phrase -v`
Expected: FAIL (old `phrase_questions` returns a list, not a dict; parses an array not an object).

- [ ] **Step 3: Rewrite `_PHRASE_SYSTEM` and `phrase_questions`**

Replace `_PHRASE_SYSTEM` (lines 212-223) and `phrase_questions` (lines 226-249) in `src/dashboard/review_questions.py`:

```python
_PHRASE_SYSTEM = (
    "You are helping a city clerk prepare pointed follow-up questions for a "
    "department's next quarterly report. You receive a JSON object with `questions` "
    "(draft questions already grounded in real data) and `signals` (the structured "
    "facts behind each). Do TWO things. (1) POLISH: rewrite each question in "
    "`questions` into one natural, specific, professional question a clerk could put "
    "directly into the report request — a neutral request for a progress update (the "
    "clerk simply lacks a newer report; do NOT assume work is blocked or failing). "
    "(2) SYNTHESIZE: from the `signals` taken together, propose 0 to 3 sharper "
    "cross-cutting questions that connect two or more facts (e.g. rising vacancies "
    "alongside a stalled goal). RULES: preserve every number, percentage, target, "
    "goal name, grant name and department name exactly — never invent, drop, or alter "
    "a fact; synthesis questions must rest only on facts present in `signals`. Return "
    "ONLY a JSON object: {\"polished\": [<same length and order as questions>], "
    "\"synthesis\": [<0-3 strings>]}. No preamble."
)


def phrase_questions(findings, settings, client=None):
    """Polish templated questions and synthesize cross-cutting ones via Haiku.

    `findings` is the list of finding dicts for one department. Returns
    {"polished": [str aligned 1:1 with findings], "synthesis": [0-3 str]}.
    Raises ValueError if the model returns the wrong `polished` count (caller
    should fall back to templated wording). Transport errors propagate.
    """
    if not findings:
        return {"polished": [], "synthesis": []}
    from src.llm.client import TrackedAnthropic
    llm = client or TrackedAnthropic(settings, call_site="dashboard.review_questions")
    questions = [f["question"] for f in findings]
    signals = [{"signal": f.get("signal"), "evidence": f.get("evidence", {})} for f in findings]
    payload = {"questions": questions, "signals": signals}
    msg = llm.messages.create(
        model=settings.profiler_model,
        max_tokens=1400,
        system=_PHRASE_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
    )
    raw = msg.content[0].text.strip()
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    out = json.loads(raw)
    polished = out.get("polished") or []
    synthesis = out.get("synthesis") or []
    if not isinstance(polished, list) or len(polished) != len(questions):
        raise ValueError(f"phrasing returned {len(polished) if isinstance(polished, list) else '?'} "
                         f"polished items, expected {len(questions)}")
    return {"polished": [str(x).strip() for x in polished],
            "synthesis": [str(x).strip() for x in synthesis if str(x).strip()][:3]}
```

- [ ] **Step 4: Update the `/questions/<dept>` route**

Replace the body of the `questions` route in `app.py` (lines 249-266, from `findings = match["findings"]` through the `return jsonify(...)`) with:

```python
        findings = match["findings"]
        templated = [f["question"] for f in findings]
        key = hashlib.sha256(json.dumps(templated, ensure_ascii=False).encode()).hexdigest()

        polished = True
        if key in _questions_cache:
            worded, synthesis = _questions_cache[key]
        else:
            try:
                res = phrase_questions(findings, get_settings())
                worded, synthesis = res["polished"], res["synthesis"]
                _questions_cache[key] = (worded, synthesis)
            except Exception as e:
                logger.warning("Question phrasing failed, using templated: %s", e)
                worded, synthesis, polished = templated, [], False

        out = [{"question": w, "signal": f["signal"], "priority": f.get("priority"),
                "evidence": f["evidence"]}
               for w, f in zip(worded, findings)]
        return jsonify({"department": match["department"], "questions": out,
                        "synthesis": synthesis, "polished": polished})
```

Also update the cache-type comment/annotation at `app.py:230`:

```python
# findings-hash -> (polished_questions, synthesis_questions)
_questions_cache: dict[str, tuple[list[str], list[str]]] = {}
```

- [ ] **Step 5: Update route tests to the new shape**

In `tests/dashboard/test_questions_route.py`:
- In `test_polishes_once_then_serves_from_cache`, change `fake_phrase` to return the dict and assert synthesis passes through:

```python
    def fake_phrase(findings, settings, client=None):
        calls["n"] += 1
        return {"polished": [f["question"] + " [polished]" for f in findings],
                "synthesis": ["Cross-cutting question?"]}
    monkeypatch.setattr(rqmod, "phrase_questions", fake_phrase)
```
  and after the first request add:
```python
    assert body["synthesis"] == ["Cross-cutting question?"]
    assert body["questions"][0]["priority"] == "high"
```

- In `test_falls_back_to_templated_on_phrasing_error`, add after the existing asserts:
```python
    assert body["synthesis"] == []
```

- In `test_unknown_department_returns_empty`, change the monkeypatch stub to the dict shape:
```python
    monkeypatch.setattr(rqmod, "phrase_questions", lambda *a, **k: {"polished": [], "synthesis": []})
```

- [ ] **Step 6: Run all dashboard tests to verify pass**

Run: `python -m pytest tests/dashboard/ -v`
Expected: PASS (all review-questions and questions-route tests).

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/review_questions.py app.py tests/dashboard/test_review_questions.py tests/dashboard/test_questions_route.py
git commit -m "feat(next-quarter): LLM pass polishes + synthesizes cross-cutting questions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Frontend — all departments, priority order, soft cap, synthesis

Update `templates/redesign.html`: register the new signals, list every department, render findings in priority order with a soft cap ("show all N"), render synthesis questions, and update copy.

**Files:**
- Modify: `templates/redesign.html` (line 305 subtitle; lines 926-970 `Q_SIG`/`applyPolish`/`renderQuestions`)

**Interfaces:**
- Consumes: `D.review_questions = {period, departments:[{department, findings:[{signal, question, priority, evidence}]}]}`; `/questions/<dept>` → `{questions:[{question,...}], synthesis:[str], polished:bool}`.

- [ ] **Step 1: Update the header subtitle (line 305)**

Replace the `<p class="psub">…</p>` text with:

```html
<p class="psub">Data-grounded follow-ups to put into each department's next quarterly report — built from prior goals, open vacancies, active grants, budget pace, and departments behind on filing.</p>
```

- [ ] **Step 2: Replace `Q_SIG` (line 927)**

```javascript
const Q_SIG={
  goal_stalled:{lbl:'Stalled goal',col:'#a0302a'},
  goal_no_progress:{lbl:'No reported progress',col:'#c4691e'},
  goal_in_progress:{lbl:'Goal follow-up',col:'#3f6f52'},
  goal_completed:{lbl:'Completed goal',col:'#5a7a6a'},
  vacancy:{lbl:'Open vacancies',col:'#7a5c1e'},
  grant:{lbl:'Grant status',col:'#4a5a8a'},
  quiet_department:{lbl:'Behind on filing',col:'#a0302a'},
  budget_pace:{lbl:'Budget off pace',col:'#16344f'},
  synthesis:{lbl:'Cross-cutting',col:'#6b4a8a'}
};
```

- [ ] **Step 3: Replace `applyPolish` (lines 929-934) to also render synthesis**

```javascript
let _qData={};   // dept display name -> {polished:[...], synthesis:[...]} (client cache)
function applyPolish(dept,data){
  if(state.qDept!==dept) return;                     // user switched away before it returned
  const texts=$('#q-list').querySelectorAll('.qtext'), btns=$('#q-list').querySelectorAll('.qcopy');
  const worded=data.polished||[];
  if(worded.length===texts.length){
    worded.forEach((w,i)=>{ texts[i].textContent=w; if(btns[i]) btns[i].dataset.q=w; });
  }
  // (re)render synthesis cards once, appended after the department's questions
  $('#q-list').querySelectorAll('.qsynth').forEach(n=>n.remove());
  const synth=data.synthesis||[];
  if(synth.length){
    const wrap=document.createElement('div');
    wrap.className='qsynth';
    wrap.style.cssText='display:flex;flex-direction:column;gap:10px;margin-top:8px';
    wrap.innerHTML=synth.map(q=>card({signal:'synthesis',question:q})).join('');
    $('#q-list').appendChild(wrap);
    wrap.querySelectorAll('.qcopy').forEach(b=>b.onclick=()=>{ if(navigator.clipboard) navigator.clipboard.writeText(b.dataset.q); const o=b.textContent; b.textContent='✓'; setTimeout(()=>b.textContent=o,1200); });
  }
}
```

- [ ] **Step 4: Replace `renderQuestions` (lines 935-970)**

`card` is hoisted to module scope (used by both `renderQuestions` and `applyPolish`); soft cap folds findings beyond 8 per department behind a "Show all N" button.

```javascript
const Q_CAP=8;
function card(f){ const sg=Q_SIG[f.signal]||{lbl:f.signal,col:'#8a867d'};
  return `<div class="card" style="padding:15px 18px"><div style="display:flex;align-items:flex-start;gap:12px">
    <span style="width:7px;height:7px;border-radius:2px;background:${sg.col};margin-top:6px;flex-shrink:0"></span>
    <div style="flex:1;min-width:0"><div style="font-size:9.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:${sg.col};margin-bottom:5px">${sg.lbl}</div>
    <div class="qtext" style="font-size:14px;line-height:1.5;color:#1c1b19">${esc(f.question)}</div></div>
    <button class="qcopy" data-q="${esc(f.question)}" title="Copy question" style="flex-shrink:0;background:none;border:1px solid rgba(28,27,25,.12);border-radius:7px;padding:5px 9px;cursor:pointer;color:#8a867d;font-size:13px">⧉</button></div></div>`;
}
function renderQuestions(){
  const rq=D.review_questions||{period:null,departments:[]};
  const depts=rq.departments||[];
  const sel=$('#q-dept'), el=$('#q-list'), copyBtn=$('#q-copyall');
  const cur=state.qDept||'All';
  sel.innerHTML=`<option value="All">All departments</option>`+depts.map(d=>`<option value="${esc(d.department)}">${esc(d.department)}</option>`).join('');
  sel.value=[...sel.options].some(o=>o.value===cur)?cur:'All';
  state.qDept=sel.value;
  if(!depts.length){ el.innerHTML='<div class="empty">No items flagged right now — tracked goals show recent progress, staffing and grants are current, and budgets are on pace.</div>'; $('#q-count').textContent=''; copyBtn.style.display='none'; return; }
  const shown = state.qDept==='All' ? depts : depts.filter(d=>d.department===state.qDept);
  const totalQ = shown.reduce((s,d)=>s+d.findings.length,0);
  $('#q-count').textContent=`${totalQ} question${totalQ===1?'':'s'}`+(rq.period?` · for the report after ${esc(rq.period)}`:'');
  el.innerHTML=shown.map((d,di)=>{
    const head=state.qDept==='All'?`<div style="display:flex;align-items:baseline;justify-content:space-between;margin:10px 0 6px"><h2 class="sec" style="font-size:16px">${esc(d.department)}</h2><span style="font-size:12px;color:#a7a298">${d.findings.length} question${d.findings.length===1?'':'s'}</span></div>`:'';
    const vis=d.findings.slice(0,Q_CAP).map(card).join('');
    const rest=d.findings.slice(Q_CAP);
    const more=rest.length?`<div class="qhidden" data-di="${di}" style="display:none;flex-direction:column;gap:10px;margin-top:10px">${rest.map(card).join('')}</div><button class="qshowall" data-di="${di}" style="align-self:flex-start;margin-top:8px;background:none;border:none;color:#16344f;font-size:12.5px;font-weight:600;cursor:pointer">Show all ${d.findings.length}</button>`:'';
    return head+`<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:8px">${vis}${more}</div>`;
  }).join('');
  el.querySelectorAll('.qshowall').forEach(b=>b.onclick=()=>{ const box=el.querySelector('.qhidden[data-di="'+b.dataset.di+'"]'); if(box){ box.style.display='flex'; b.style.display='none'; } });
  el.querySelectorAll('.qcopy').forEach(b=>b.onclick=()=>{ if(navigator.clipboard) navigator.clipboard.writeText(b.dataset.q); const o=b.textContent; b.textContent='✓'; setTimeout(()=>b.textContent=o,1200); });
  copyBtn.style.display='';
  copyBtn.onclick=()=>{ const all=[...$('#q-list').querySelectorAll('.qtext')].map(t=>'• '+t.textContent).join('\n'); if(navigator.clipboard) navigator.clipboard.writeText(all); copyBtn.textContent='Copied ✓'; setTimeout(()=>copyBtn.textContent='Copy all',1400); };
  // On-demand LLM polish + synthesis for a single selected department only (cached;
  // never for "All" — that would fan out one call per department). Respects the funds rule.
  if(state.qDept!=='All'){
    const dept=state.qDept;
    if(_qData[dept]) applyPolish(dept,_qData[dept]);
    else fetch('/questions/'+encodeURIComponent(dept)).then(r=>r.json()).then(j=>{
      if(j&&Array.isArray(j.questions)){ _qData[dept]={polished:(j.polished?j.questions.map(x=>x.question):null)||[],synthesis:j.synthesis||[]}; applyPolish(dept,_qData[dept]); }
    }).catch(()=>{});
  }
}
```

- [ ] **Step 5: Syntax-check the template's JavaScript**

Run:
```bash
python - <<'PY'
import re, pathlib, subprocess, tempfile, os
html = pathlib.Path("templates/redesign.html").read_text()
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
src = "\n".join(scripts)
f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False); f.write(src); f.close()
print(subprocess.run(["node","--check",f.name]).returncode)
os.unlink(f.name)
PY
```
Expected: prints `0` (no syntax errors).

- [ ] **Step 6: Jinja parse check**

Run:
```bash
python - <<'PY'
from jinja2 import Environment, FileSystemLoader
Environment(loader=FileSystemLoader("templates")).get_template("redesign.html")
print("jinja ok")
PY
```
Expected: prints `jinja ok`.

- [ ] **Step 7: Commit**

```bash
git add templates/redesign.html
git commit -m "feat(next-quarter): all-dept picker, priority order, soft cap, synthesis UI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Full regression + verify

Run the whole dashboard suite and eyeball the rendered tab against real data (read-only; no ingest, no forced LLM spend — opening one department triggers at most one cached Haiku call, which is the intended on-demand behavior).

**Files:** none (verification only).

- [ ] **Step 1: Run the full dashboard test suite**

Run: `python -m pytest tests/dashboard/ -v`
Expected: PASS (all).

- [ ] **Step 2: Verify build() shape carries priority**

Run:
```bash
python - <<'PY'
from tests.dashboard.test_review_questions import _FakeStore, GOALS, PERIOD, BUDGET, _goal
from src.dashboard.review_questions import ReviewQuestions
store=_FakeStore({GOALS:[_goal(1,"Bureau of Fire",2026,"Q1","Reduce response time",target="< 6 min")],PERIOD:[],BUDGET:[]})
out=ReviewQuestions(store).build()
f=out["departments"][0]["findings"][0]
assert set(f)=={"signal","department","question","priority","evidence"}, f
print("shape ok:", f["signal"], f["priority"])
PY
```
Expected: prints `shape ok: goal_no_progress high`.

- [ ] **Step 3: (Optional, requires DB) Launch and eyeball the tab**

Only if a populated DB is available. Use the project's run skill / existing launch command, open the "Next quarter" tab, confirm: more departments listed than before; each shows priority-ordered questions; a long department folds behind "Show all N"; selecting one department swaps to polished wording and appends a "Cross-cutting" card. Do **not** click through many departments unnecessarily (each first open is one cached LLM call).

- [ ] **Step 4: Finalize**

No commit needed if steps 1-2 pass and no code changed. Use the `superpowers:finishing-a-development-branch` skill to decide merge/PR.

---

## Self-Review

**Spec coverage:**
- Broadened detection (5 families) → Tasks 1-4. ✓
- `priority` field + ranking + soft cap → Task 1 (`priority`, sort), Task 6 (cap/"show all"). ✓
- Full canonical department picker → Task 6 Step 4 (picker lists all `departments`). ✓
- On-demand LLM polish **+ synthesis**, cached, never in build() → Task 5. ✓
- Clerk-set status demotes not suppresses → Task 1 Step 1 (3 tests updated) + `_goal_findings`. ✓
- No generic filler for zero-signal depts → Task 6 empty-state copy; `build()` only emits depts with findings. ✓
- Error handling: templated fallback + `polished:false` + `synthesis:[]` on any LLM/parse/count error → Task 5 route + `phrase_questions` ValueError path. ✓
- Testing per spec (per-signal, ranking, endpoint miss/hit/fallback/mismatch, synthesis, node --check, jinja) → Tasks 1-6. ✓
- Cost constraint: detection deterministic; `phrase_questions` only from route → Global Constraints + Task 5. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every command has expected output. ✓

**Type consistency:** `_goal_findings` returns a 2-tuple `(findings_by_key, meta)` — `build()` and all tests use exactly that. `phrase_questions(findings,...) -> {"polished","synthesis"}` used identically in route + tests. Finding keys `{signal,department,question,priority,evidence}` consistent across all signal methods, route, and frontend `card()`. `_questions_cache` value is a `(worded, synthesis)` tuple in both write and read paths. `card`/`applyPolish` share module scope in the template. ✓
