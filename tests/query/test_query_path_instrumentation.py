from types import SimpleNamespace

from src.config import Settings
from src.llm.client import TrackedAnthropic
from src.models import QueryResponse
from src.query.classifier import QueryClassifier
from src.query.synthesizer import Synthesizer


class FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text=self._text)],
        )


class FakeClient:
    def __init__(self, text):
        self.messages = FakeMessages(text)


def _tracked(text, call_site, recorded):
    return TrackedAnthropic(Settings(anthropic_api_key="x"), call_site=call_site,
                            client=FakeClient(text), sink=recorded.append)


def test_default_synthesizer_client_has_call_site():
    s = Synthesizer(Settings(anthropic_api_key="x"))
    assert s.client.call_site == "query.synthesizer"


def test_default_classifier_client_has_call_site():
    c = QueryClassifier(Settings(anthropic_api_key="x"))
    assert c.client.call_site == "query.classifier"


def test_synthesize_records_usage_with_query_id():
    recorded = []
    llm = _tracked("Final answer [Source: x, Section: y]", "query.synthesizer", recorded)
    s = Synthesizer(Settings(anthropic_api_key="x"), llm=llm)
    # one vector result so context is non-empty
    results = [SimpleNamespace(store="vector", error=None,
                               chunks=[{"payload": {"text": "t", "source_file": "f"},
                                        "chunk_id": None}],
                               sql_rows=None, graph_data=None)]
    resp = QueryResponse(query_id="55555555-5555-5555-5555-555555555555",
                         question="q", answer="", timestamp="t")
    s.synthesize("q", results, resp)
    assert len(recorded) == 1
    assert recorded[0].query_id == "55555555-5555-5555-5555-555555555555"
    assert recorded[0].call_site == "query.synthesizer"


def test_classify_records_usage_with_query_id():
    recorded = []
    llm = _tracked('{"sources": ["vector"], "execution": "parallel"}', "query.classifier", recorded)
    c = QueryClassifier(Settings(anthropic_api_key="x"), llm=llm)
    c.classify("who runs public works?", query_id="66666666-6666-6666-6666-666666666666")
    assert len(recorded) == 1
    assert recorded[0].query_id == "66666666-6666-6666-6666-666666666666"
    assert recorded[0].call_site == "query.classifier"
