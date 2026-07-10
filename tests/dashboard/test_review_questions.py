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


def test_phrase_questions_returns_aligned_list():
    llm = _FakeLLM('["Q1 polished", "Q2 polished"]')
    out = phrase_questions(["Q1", "Q2"], _Settings(), client=llm)
    assert out == ["Q1 polished", "Q2 polished"] and llm.calls == 1


def test_phrase_questions_empty_makes_no_call():
    llm = _FakeLLM("[]")
    assert phrase_questions([], _Settings(), client=llm) == [] and llm.calls == 0


def test_phrase_questions_count_mismatch_raises():
    llm = _FakeLLM('["only one"]')
    with pytest.raises(ValueError):
        phrase_questions(["Q1", "Q2"], _Settings(), client=llm)
