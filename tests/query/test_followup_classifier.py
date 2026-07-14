from types import SimpleNamespace

from src.config import Settings
from src.llm.client import TrackedAnthropic
from src.query.classifier import (
    QueryClassifier,
    _build_prior_conversation,
    _CLASSIFY_PROMPT,
    _parse_plan,
)


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


class _CapturingMessages:
    def __init__(self):
        self.prompts = []

    def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=5, output_tokens=2,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text='{"sources": ["sql"], "execution": "parallel"}')],
        )


class _CapturingClient:
    def __init__(self):
        self.messages = _CapturingMessages()


def _classifier_with_capture():
    cfg = Settings(anthropic_api_key="x")
    rec = _CapturingClient()
    llm = TrackedAnthropic(cfg, call_site="query.classifier", client=rec, sink=lambda r: None)
    return QueryClassifier(cfg, llm=llm), rec


def test_prompt_defines_resolved_question_and_followup_guard():
    # The prompt must instruct the model to emit resolved_question and to only
    # carry context when the question actually depends on it.
    assert "resolved_question" in _CLASSIFY_PROMPT
    assert "self-contained" in _CLASSIFY_PROMPT.lower()


def test_build_prior_conversation_empty_when_no_history():
    assert _build_prior_conversation(None) == ""
    assert _build_prior_conversation([]) == ""


def test_build_prior_conversation_includes_turns():
    block = _build_prior_conversation([
        {"question": "fire dept budget 2024?", "answer": "$5M"},
    ])
    assert "fire dept budget 2024?" in block
    assert "$5M" in block


# The prompt's static FOLLOW-UPS rule references the phrase "Prior conversation",
# so the injected block is identified by its unique header, not the bare phrase.
_BLOCK_HEADER = "Prior conversation (most recent last):"


def test_classify_without_history_omits_prior_conversation_block():
    clf, rec = _classifier_with_capture()
    clf.classify("who directs public works?")
    prompt = rec.messages.prompts[0]
    assert _BLOCK_HEADER not in prompt


def test_classify_with_history_injects_prior_conversation():
    clf, rec = _classifier_with_capture()
    clf.classify(
        "what about 2023?",
        history=[{"question": "fire dept budget 2024?", "answer": "$5M"}],
    )
    prompt = rec.messages.prompts[0]
    assert _BLOCK_HEADER in prompt
    assert "fire dept budget 2024?" in prompt
