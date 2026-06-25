from types import SimpleNamespace

from src.config import Settings
from src.evaluation.evaluator import Evaluator
from src.evaluation.suite import EvaluationSuite
from src.llm.client import TrackedAnthropic
from src.models import QueryResponse


class FakeLLMMessages:
    def create(self, **kwargs):
        # Valid evaluator JSON so _parse_score yields a real score
        text = ('{"retrieval_score": 4, "accuracy_score": 5, "completeness_score": 4, '
                '"retrieval_failure": false, "hallucination_detected": false, "reasoning": "ok"}')
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text=text)],
        )


class FakeLLM:
    def __init__(self):
        self.messages = FakeLLMMessages()


class FakePipeline:
    def ask(self, question, log_query=False):
        return QueryResponse(query_id="77777777-7777-7777-7777-777777777777",
                             question=question, answer="Some answer", timestamp="t")


class FakeStore:
    def get_evaluation_suite(self):
        return [{"id": 1, "question": "Who is the Director of Public Works?",
                 "expected_answer": "David West", "store_type": "graph"}]

    def save_evaluation_result(self, run_id, result):
        pass


def test_suite_run_invokes_evaluator_without_typeerror():
    cfg = Settings(anthropic_api_key="x")
    # real Evaluator, but backed by a fake LLM + no-op sink so no network/DB
    evaluator = Evaluator(cfg, llm=TrackedAnthropic(cfg, call_site="evaluation.evaluator",
                                                    client=FakeLLM(), sink=lambda r: None))
    suite = EvaluationSuite(FakeStore(), cfg)
    results = suite.run(FakePipeline(), evaluator)
    # Before the fix, the wrong kwarg raises TypeError, is swallowed, and results is empty.
    assert len(results) == 1
    assert results[0].accuracy_score == 5.0
