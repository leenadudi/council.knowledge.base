from unittest.mock import MagicMock

from src.config import Settings
from src.models import QueryPlan, RetrievalResult
from src.query.pipeline import QueryPipeline, _MAX_HISTORY_TURNS


def _pipeline_with_mocks(plan: QueryPlan):
    pipe = QueryPipeline.__new__(QueryPipeline)
    # Real Settings so clarity thresholds are numeric (assess_retrieval runs for real).
    pipe.cfg = Settings(anthropic_api_key="x")
    pipe.classifier = MagicMock()
    pipe.classifier.classify.return_value = plan
    pipe.retriever = MagicMock()
    pipe.retriever.retrieve.return_value = [
        RetrievalResult(store="vector", chunks=[{"score": 0.9, "payload": {}}]),
    ]
    pipe.retriever.fallback_retrieve.return_value = []
    pipe.synthesizer = MagicMock()
    pipe.synthesizer.synthesize.side_effect = (
        lambda q, results, resp: resp
    )
    pipe.sql_store = MagicMock()
    return pipe


def test_ask_without_history_passes_none_and_original_question():
    plan = QueryPlan(sources=["vector"], execution="parallel")
    pipe = _pipeline_with_mocks(plan)
    pipe.ask("who directs public works?", log_query=False)
    pipe.classifier.classify.assert_called_once()
    assert pipe.classifier.classify.call_args.kwargs.get("history") is None
    # synthesizer receives the original question (resolved_question is None)
    assert pipe.synthesizer.synthesize.call_args.args[0] == "who directs public works?"


def test_ask_uses_resolved_question_for_synthesis():
    plan = QueryPlan(
        sources=["sql"], execution="parallel",
        resolved_question="fire dept allocation in 2023?",
    )
    pipe = _pipeline_with_mocks(plan)
    pipe.ask(
        "what about 2023?",
        history=[{"question": "fire dept allocation in 2024?", "answer": "$5M"}],
        log_query=False,
    )
    # synthesizer sees the resolved standalone question, not "what about 2023?"
    assert pipe.synthesizer.synthesize.call_args.args[0] == "fire dept allocation in 2023?"


def test_ask_caps_history_to_last_two_turns():
    plan = QueryPlan(sources=["vector"], execution="parallel")
    pipe = _pipeline_with_mocks(plan)
    history = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(5)]
    pipe.ask("follow up", history=history, log_query=False)
    passed = pipe.classifier.classify.call_args.kwargs["history"]
    assert len(passed) == _MAX_HISTORY_TURNS
    assert passed == history[-_MAX_HISTORY_TURNS:]


def test_ask_preserves_original_question_on_response():
    plan = QueryPlan(
        sources=["sql"], execution="parallel",
        resolved_question="fire dept allocation in 2023?",
    )
    pipe = _pipeline_with_mocks(plan)
    resp = pipe.ask(
        "what about 2023?",
        history=[{"question": "fire dept allocation in 2024?", "answer": "$5M"}],
        log_query=False,
    )
    assert resp.question == "what about 2023?"
