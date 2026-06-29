"""
Unit tests for src/evaluation/ingestion_judge.py — use a fake client so
no API key is required. These are NOT marked integration.
"""
from __future__ import annotations

import json
import pytest


# ---------------------------------------------------------------------------
# Fake LLM client infrastructure
# ---------------------------------------------------------------------------

class _FakeMsg:
    """Mimics anthropic.types.Message with a single text content block."""
    def __init__(self, text: str):
        self.content = [type("C", (), {"text": text})()]


class _FakeMessages:
    def __init__(self, response_text: str):
        self._text = response_text

    def create(self, **kwargs):
        return _FakeMsg(self._text)


class _FakeClient:
    """Minimal stub that satisfies `client.messages.create(**kwargs)`."""
    def __init__(self, response_text: str):
        self.messages = _FakeMessages(response_text)


# ---------------------------------------------------------------------------
# Tests for judge_extraction — happy path
# ---------------------------------------------------------------------------

def test_judge_extraction_parses_valid_json():
    """A well-formed JSON response from the LLM is parsed to the right dict."""
    from src.evaluation.ingestion_judge import judge_extraction

    judge_json = json.dumps({
        "score": 5,
        "complete": True,
        "hallucinated": False,
        "reasoning": "All facts are present and accurate.",
    })
    client = _FakeClient(judge_json)

    result = judge_extraction(
        source_text="RESOLUTION NO 2026-R-12 authorises $40,000 to ABC Corp.",
        extracted={"resolutions": [{"resolution_number": "2026-R-12", "amount": 40000.0}]},
        expected_notes="Should capture the $40,000 award.",
        client=client,
    )

    assert result["score"] == 5
    assert result["complete"] is True
    assert result["hallucinated"] is False
    assert "accurate" in result["reasoning"]


def test_judge_extraction_parses_json_embedded_in_prose():
    """The judge must handle responses where JSON is wrapped in prose text."""
    from src.evaluation.ingestion_judge import judge_extraction

    judge_json = json.dumps({
        "score": 4,
        "complete": True,
        "hallucinated": False,
        "reasoning": "Good extraction.",
    })
    response_with_prose = f"Here is my evaluation:\n\n{judge_json}\n\nHope that helps."
    client = _FakeClient(response_with_prose)

    result = judge_extraction(
        source_text="Some document text",
        extracted={"resolutions": []},
        client=client,
    )

    assert result["score"] == 4
    assert result["hallucinated"] is False


# ---------------------------------------------------------------------------
# Tests for judge_extraction — never-raise / safe-default path
# ---------------------------------------------------------------------------

def test_judge_extraction_returns_safe_default_on_garbage_response():
    """When the LLM returns unparseable garbage, judge_extraction must NOT raise
    and must return the safe default dict."""
    from src.evaluation.ingestion_judge import judge_extraction

    client = _FakeClient("I cannot provide a score right now. Try again later.")

    result = judge_extraction(
        source_text="Some document",
        extracted={},
        client=client,
    )

    assert result["score"] == 0
    assert result["complete"] is False
    assert result["hallucinated"] is True
    assert result["reasoning"] == "unparseable"


def test_judge_extraction_returns_safe_default_on_empty_response():
    """Empty string from LLM → safe default, no raise."""
    from src.evaluation.ingestion_judge import judge_extraction

    client = _FakeClient("")

    result = judge_extraction(source_text="doc", extracted={}, client=client)

    assert result["score"] == 0
    assert result["reasoning"] == "unparseable"


def test_judge_extraction_returns_safe_default_on_partial_json():
    """Truncated JSON → safe default, no raise."""
    from src.evaluation.ingestion_judge import judge_extraction

    client = _FakeClient('{"score": 3, "complete": tru')  # truncated

    result = judge_extraction(source_text="doc", extracted={}, client=client)

    assert result["score"] == 0
    assert result["reasoning"] == "unparseable"
