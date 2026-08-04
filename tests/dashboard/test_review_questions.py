# tests/dashboard/test_review_questions.py
from contextlib import contextmanager

import pytest

from src.dashboard.review_questions import ReviewQuestions, phrase_questions


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


# distinctive SQL substrings per query in review_questions.py
GOALS = "FROM goals"
PERIOD = "LIMIT 1"              # latest-period lookup on expenditures
BUDGET = "GROUP BY department"  # per-dept rollup on expenditures


def _goal(id, dept, year, q, title, target="", status=None, user_status=None):
    return {"id": id, "department": dept, "year": year, "quarter": q,
            "goal_title": title, "description": "", "target": target,
            "status": status, "user_status": user_status}


def test_empty_data_returns_no_departments():
    out = ReviewQuestions(_FakeStore({GOALS: [], PERIOD: [], BUDGET: []})).build()
    assert out == {"period": None, "departments": []}


def test_goal_with_no_progress_is_flagged():
    store = _FakeStore({
        GOALS: [_goal(1, "Bureau of Fire", 2026, "Q1", "Reduce response time", target="< 6 min", status=None)],
        PERIOD: [], BUDGET: [],
    })
    out = ReviewQuestions(store).build()
    assert len(out["departments"]) == 1
    d = out["departments"][0]
    assert d["department"] == "Bureau of Fire"
    assert [f["signal"] for f in d["findings"]] == ["goal_no_progress"]
    assert "< 6 min" in d["findings"][0]["question"]
    assert out["period"] == "Q1 2026"


def test_goal_without_target_is_still_flagged_no_progress():
    # a goal with no numeric target but no reported status is still a valid gap;
    # the question just omits the "(target: …)" clause.
    store = _FakeStore({
        GOALS: [_goal(1, "Bureau of Fire", 2026, "Q1", "Community outreach", target="", status=None)],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"]
    assert len(d) == 1
    f = d[0]["findings"][0]
    assert f["signal"] == "goal_no_progress"
    assert "target:" not in f["question"] and f["evidence"]["target"] is None


def test_goal_stalled_across_quarters_is_flagged_and_suppresses_no_progress():
    store = _FakeStore({
        GOALS: [
            _goal(1, "Bureau of Fire", 2025, "Q3", "Hire 3 EMTs", target="3 hires", status=None),
            _goal(2, "Bureau of Fire", 2025, "Q4", "Hire 3 EMTs", target="3 hires", status=None),
        ],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    # exactly one finding — stalled wins, no duplicate no_progress for the same goal
    assert [f["signal"] for f in d["findings"]] == ["goal_stalled"]
    ev = d["findings"][0]["evidence"]
    assert ev["count"] == 2
    assert ev["periods"] == ["Q3 2025", "Q4 2025"]


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


def test_clerk_user_status_demotes_no_progress_to_followup():
    store = _FakeStore({
        GOALS: [_goal(1, "Bureau of Fire", 2026, "Q1", "Reduce response time",
                      target="< 6 min", status=None, user_status="in_progress")],
        PERIOD: [], BUDGET: [],
    })
    d = ReviewQuestions(store).build()["departments"][0]
    assert [f["signal"] for f in d["findings"]] == ["goal_in_progress"]
    assert d["findings"][0]["priority"] == "medium"


def test_completed_goal_generates_no_question():
    # completed goals are not actionable follow-ups -> no question at all
    store = _FakeStore({
        GOALS: [
            _goal(1, "Bureau of Fire", 2025, "Q3", "Hire 3 EMTs", target="3", status=None),
            _goal(2, "Bureau of Fire", 2025, "Q4", "Hire 3 EMTs", target="3", status=None,
                  user_status="completed"),
        ],
        PERIOD: [], BUDGET: [],
    })
    assert ReviewQuestions(store).build()["departments"] == []


def test_budget_pace_flags_ahead_and_ignores_on_pace():
    store = _FakeStore({
        GOALS: [],
        PERIOD: [{"year": 2026, "quarter": "Q1"}],
        BUDGET: [
            {"department": "Bureau of Police", "rb": 1000.0, "ytd": 950.0},   # 95% by Q1 -> ahead
            {"department": "Public Works", "rb": 1000.0, "ytd": 250.0},       # 25% by Q1 -> on pace
        ],
    })
    d = ReviewQuestions(store).build()["departments"]
    assert len(d) == 1 and d[0]["department"] == "Bureau of Police"
    f = d[0]["findings"][0]
    assert f["signal"] == "budget_pace" and f["evidence"]["direction"] == "ahead"
    assert "95%" in f["question"]


def test_budget_pace_flags_behind():
    store = _FakeStore({
        GOALS: [],
        PERIOD: [{"year": 2026, "quarter": "Q4"}],
        BUDGET: [{"department": "Engineering", "rb": 1000.0, "ytd": 300.0}],  # 30% by Q4, expected 100%
    })
    f = ReviewQuestions(store).build()["departments"][0]["findings"][0]
    assert f["evidence"]["direction"] == "behind" and "behind pace" in f["question"]


def test_department_name_variants_merge():
    store = _FakeStore({
        GOALS: [
            _goal(1, "Parks & Recreation", 2025, "Q3", "Repave 5 courts", target="5", status=None),
            _goal(2, "Bureau of Parks & Recreation", 2025, "Q4", "Repave 5 courts", target="5", status=None),
        ],
        PERIOD: [], BUDGET: [],
    })
    out = ReviewQuestions(store).build()
    assert len(out["departments"]) == 1   # merged into one canonical department
    assert out["departments"][0]["findings"][0]["evidence"]["count"] == 2


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


RES = "FROM resolutions"
_TODAY = __import__("datetime").date(2026, 7, 29)


def _res(num, title, dept, amount=None, vendor=None, adopted=None, status="Passed"):
    import datetime as _dt
    return {"resolution_number": num, "title": title, "department": dept,
            "amount": amount, "vendor": vendor,
            "adopted_date": _dt.date.fromisoformat(adopted) if adopted else None,
            "status": status}


def _res_findings(rows):
    store = _FakeStore({GOALS: [], PERIOD: [], BUDGET: [], RES: rows})
    return ReviewQuestions(store)._resolution_findings(today=_TODAY)


def test_resolution_findings_are_wired_into_build():
    # Guard: _resolution_findings must be merged into build()'s output — not only
    # unit-tested in isolation. Regression: the generator was written and unit-
    # tested but never called from build(), so passed-resolution questions never
    # reached the dashboard. build() uses the real date.today(), so the row is
    # adopted well over _RES_MIN_AGE_DAYS in the past to always clear the age gate.
    store = _FakeStore({GOALS: [], PERIOD: [], BUDGET: [], RES: [_res(
        "9-2026", "A Resolution authorizing a professional services agreement",
        "Bureau of Fire", amount=50000.0, vendor="Acme Corp", adopted="2025-01-15")]})
    depts = ReviewQuestions(store).build()["departments"]
    signals = [f["signal"] for d in depts for f in d["findings"]]
    assert "resolution" in signals


def test_passed_contract_resolution_with_amount_is_flagged():
    out = _res_findings([_res(
        "2-2026", "A Resolution authorizing a professional services agreement for design",
        "Parks and Recreation", amount=19500.0, vendor="Floura Teeter Landscape Architects, Inc.",
        adopted="2026-02-03")])
    assert len(out) == 1
    f = next(iter(out.values()))[0]
    assert f["signal"] == "resolution" and f["priority"] == "medium"
    assert "2-2026" in f["question"] and "$19,500" in f["question"]
    assert "Floura Teeter" in f["question"] and "spent to date" in f["question"]
    assert f["evidence"]["kind"] == "contract"


def test_contract_without_amount_asks_for_value_at_low_priority():
    f = next(iter(_res_findings([_res(
        "22-2026", "A Resolution authorizing a professional services agreement",
        "Office of City Solicitor", vendor="McCormick Law Firm", adopted="2026-03-23")]).values()))[0]
    assert f["priority"] == "low" and "final contract value" in f["question"]


def test_grant_application_resolution_asks_about_award():
    f = next(iter(_res_findings([_res(
        "9-2026", "A Resolution authorizing the submission of a grant application to USDOT",
        "Engineering / Grants", amount=3000000.0, vendor="United States Department of Transportation",
        adopted="2026-01-27")]).values()))[0]
    assert f["evidence"]["kind"] == "grant_application"
    assert "award decision" in f["question"] and "$3,000,000" in f["question"]


def test_ratified_grant_resolution_asks_about_drawdown():
    f = next(iter(_res_findings([_res(
        "6-2026", "A Resolution ratifying a PennDOT Green Light-Go Grant agreement",
        "Engineering / Grants", amount=387380.0, vendor="PennDOT", adopted="2026-01-27")]).values()))[0]
    assert f["evidence"]["kind"] == "grant_award"
    assert "drawn down" in f["question"] or "spent" in f["question"]


def test_budget_reallocation_resolution_asks_about_posting():
    f = next(iter(_res_findings([_res(
        "3-2026", "A Resolution approving the First Proposed 2026 Budget Reallocation",
        "Finance", adopted="2026-01-10")]).values()))[0]
    assert f["evidence"]["kind"] == "budget" and "reallocat" in f["question"]


def test_appointment_resolution_is_skipped():
    assert _res_findings([_res(
        "17-2026", "A Resolution confirming the appointment of Jane Doe as Director",
        "Building and Housing", adopted="2026-03-10")]) == {}


def test_unpassed_or_statusless_resolution_is_skipped():
    rows = [
        _res("16-2026", "A Resolution authorizing a lease agreement", "Public Works",
             adopted="2026-03-01", status=None),
        _res("30-2026", "A Resolution authorizing a lease agreement", "Public Works",
             adopted="2026-03-01", status="Tabled"),
    ]
    assert _res_findings(rows) == {}


def test_recent_resolution_is_not_asked_about_yet():
    # adopted 10 days before "today" — too fresh for an implementation question
    assert _res_findings([_res(
        "40-2026", "A Resolution authorizing a professional services agreement",
        "Public Works", adopted="2026-07-19")]) == {}


def test_resolution_findings_capped_and_ranked_by_amount():
    rows = [_res(f"{i}-2026", "A Resolution authorizing a professional services agreement",
                 "Public Works", amount=float(i * 1000), vendor=f"Vendor {i}",
                 adopted="2026-02-01") for i in range(1, 6)]   # 5 resolutions
    fs = next(iter(_res_findings(rows).values()))
    assert len(fs) == 3
    assert "$5,000" in fs[0]["question"]      # highest amount first


# ── phrasing pass (mocked LLM — no real spend) ───────────────────────────────

class _FakeMsg:
    def __init__(self, text): self.content = [type("C", (), {"text": text})()]


class _FakeLLM:
    def __init__(self, text): self._text = text; self.calls = 0
    @property
    def messages(self): return self
    def create(self, **kw): self.calls += 1; return _FakeMsg(self._text)


class _Settings:
    profiler_model = "claude-haiku-4-5"


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
