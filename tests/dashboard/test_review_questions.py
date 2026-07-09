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


def _goal(id, dept, year, q, title, target="", status=None):
    return {"id": id, "department": dept, "year": year, "quarter": q,
            "goal_title": title, "description": "", "target": target, "status": status}


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


def test_goal_without_target_is_not_flagged_no_progress():
    store = _FakeStore({
        GOALS: [_goal(1, "Bureau of Fire", 2026, "Q1", "Community outreach", target="", status=None)],
        PERIOD: [], BUDGET: [],
    })
    assert ReviewQuestions(store).build()["departments"] == []


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


def test_goal_with_changing_status_is_not_stalled():
    store = _FakeStore({
        GOALS: [
            _goal(1, "Bureau of Fire", 2025, "Q3", "Hire 3 EMTs", target="3 hires", status="1 of 3 hired"),
            _goal(2, "Bureau of Fire", 2025, "Q4", "Hire 3 EMTs", target="3 hires", status="2 of 3 hired"),
        ],
        PERIOD: [], BUDGET: [],
    })
    # progress is being reported (status changes) -> not a gap
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
