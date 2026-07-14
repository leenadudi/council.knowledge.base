from src.query.classifier import _parse_plan


def test_parse_plan_defaults_resolved_question_to_none():
    plan = _parse_plan('{"sources": ["vector"], "execution": "parallel"}')
    assert plan.resolved_question is None


def test_parse_plan_reads_resolved_question_when_present():
    raw = (
        '{"sources": ["sql"], "execution": "parallel", '
        '"resolved_question": "what was the fire dept allocation in 2023?"}'
    )
    plan = _parse_plan(raw)
    assert plan.resolved_question == "what was the fire dept allocation in 2023?"
