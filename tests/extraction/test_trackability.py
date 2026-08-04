# tests/extraction/test_trackability.py
import json

import pytest

from src.config import Settings
from src.extraction.trackability import (
    NEEDS_REVIEW, LogicalGoal, TrackabilityResult, apply_result, build_prompt,
    classify_goal, collapse_goals, goal_group_key, parse_result,
)


def _row(id, dept="Bureau of Police", title="Reduce gun violence", **kw):
    base = dict(id=id, department=dept, goal_title=title, year=2025, quarter="Q1",
                description="", target="", status="", trackability=None,
                trackability_source=None)
    base.update(kw)
    return base


# ---- grouping -----------------------------------------------------------

def test_groups_across_quarters_and_dept_variants():
    rows = [
        _row(1, dept="Parks & Recreation", quarter="Q1"),
        _row(2, dept="Bureau of Parks & Recreation", quarter="Q2"),
        _row(3, dept="Parks & Recreation", title="A different goal"),
    ]
    goals = collapse_goals(rows)
    assert len(goals) == 2
    g = next(g for g in goals if g.title == "Reduce gun violence")
    assert sorted(g.row_ids) == [1, 2]


def test_title_normalization_collapses_whitespace_and_case():
    key1 = goal_group_key("Bureau of Fire", "Reduce  Response\nTimes ")
    key2 = goal_group_key("Fire", "reduce response times")
    assert key1 == key2


def test_blank_titles_are_skipped():
    assert collapse_goals([_row(1, title=""), _row(2, title="  ")]) == []


def test_keeps_richest_context_and_last_two_notes():
    rows = [
        _row(1, quarter="Q1", description="short", status="note one"),
        _row(2, quarter="Q2", description="a much longer description", status="note two"),
        _row(3, quarter="Q3", target="57%", status="note three"),
    ]
    (g,) = collapse_goals(rows)
    assert g.description == "a much longer description"
    assert g.target == "57%"
    assert g.status_notes == ["note two", "note three"]  # most recent 2, in order


def test_human_label_flag_carries_to_group():
    rows = [_row(1), _row(2, trackability="aspiration", trackability_source="human")]
    (g,) = collapse_goals(rows)
    assert g.has_human_label and g.trackability == "aspiration"


# ---- parsing ------------------------------------------------------------

def test_parse_result_accepts_fenced_json():
    raw = 'Here you go:\n```json\n{"label": "measurable", "confidence": 0.9, "rationale": "has a % target", "metric_hint": "clearance rate 57%", "deadline": null}\n```'
    r = parse_result(raw)
    assert r.label == "measurable" and r.confidence == 0.9
    assert r.metric_hint == "clearance rate 57%" and r.deadline is None


def test_parse_result_rejects_invalid_label():
    with pytest.raises(ValueError):
        parse_result(json.dumps({"label": "vibes", "confidence": 0.9}))


def test_parse_result_clamps_confidence():
    assert parse_result(json.dumps({"label": "milestone", "confidence": 1.7})).confidence == 1.0


def test_low_confidence_quarantines_to_needs_review():
    r = TrackabilityResult(label="aspiration", confidence=0.5)
    assert r.stored_label(threshold=0.8) == NEEDS_REVIEW
    assert r.stored_label(threshold=0.4) == "aspiration"


def test_prompt_contains_goal_fields_and_rubric():
    g = LogicalGoal(department="Bureau of Police", title="Reduce gun violence",
                    target="10 fewer incidents", status_notes=["7 saturation details"])
    p = build_prompt(g)
    for needle in ("Reduce gun violence", "10 fewer incidents", "7 saturation details",
                   '"measurable"', '"milestone"', '"aspiration"'):
        assert needle in p


# ---- classify (fake client) ---------------------------------------------

class _FakeMsg:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]


class _FakeMessages:
    def __init__(self, replies): self._replies = list(replies); self.calls = 0
    def create(self, **kwargs):
        self.calls += 1
        return _FakeMsg(self._replies.pop(0))


class _FakeClient:
    def __init__(self, replies): self.messages = _FakeMessages(replies)


def _settings():
    return Settings(trackability_model="claude-haiku-4-5",
                    trackability_confidence_threshold=0.8)


def test_classify_goal_retries_flaky_json_then_succeeds():
    good = json.dumps({"label": "milestone", "confidence": 0.85, "rationale": "deadline"})
    client = _FakeClient(["not json at all", good])
    r = classify_goal(LogicalGoal(department="IT", title="Complete RMS conversion"),
                      client=client, settings=_settings())
    assert r.label == "milestone" and client.messages.calls == 2


def test_classify_goal_raises_after_exhausted_retries():
    client = _FakeClient(["nope", "nope", "nope"])
    with pytest.raises(RuntimeError):
        classify_goal(LogicalGoal(department="IT", title="X"),
                      client=client, settings=_settings())


# ---- persistence guard --------------------------------------------------

class _FakeCursor:
    def __init__(self): self.executed = []
    def execute(self, sql, params=None): self.executed.append((sql, params))


def test_apply_result_updates_group_and_guards_human_rows():
    cur = _FakeCursor()
    goal = LogicalGoal(department="Fire", title="X", row_ids=[4, 9, 12])
    stored = apply_result(cur, goal, TrackabilityResult("measurable", 0.92, "r"), 0.8)
    assert stored == "measurable"
    sql, params = cur.executed[0]
    assert "trackability_source='ai'" in sql
    assert "<> 'human'" in sql          # never clobber a clerk's label
    assert params[0] == "measurable" and params[-1] == [4, 9, 12]


def test_apply_result_stores_needs_review_below_threshold():
    cur = _FakeCursor()
    goal = LogicalGoal(department="Fire", title="X", row_ids=[1])
    stored = apply_result(cur, goal, TrackabilityResult("aspiration", 0.6, "r"), 0.8)
    assert stored == NEEDS_REVIEW
    assert cur.executed[0][1][0] == NEEDS_REVIEW
