from types import SimpleNamespace

from src.config import Settings
from src.llm.client import TrackedAnthropic
from src.query.classifier import QueryClassifier


class RecordingMessages:
    def __init__(self):
        self.models = []

    def create(self, **kwargs):
        self.models.append(kwargs.get("model"))
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=5, output_tokens=2,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text='{"sources": ["vector"], "execution": "parallel"}')],
        )


class RecordingClient:
    def __init__(self):
        self.messages = RecordingMessages()


def test_classifier_uses_query_classifier_model_setting():
    cfg = Settings(anthropic_api_key="x", query_classifier_model="claude-haiku-4-5")
    rec = RecordingClient()
    llm = TrackedAnthropic(cfg, call_site="query.classifier", client=rec, sink=lambda r: None)
    QueryClassifier(cfg, llm=llm).classify("who runs public works?")
    assert rec.messages.models == ["claude-haiku-4-5"]


def test_classifier_model_defaults_to_haiku():
    # Phase 3 eval gate confirmed Haiku holds accuracy at lower cost.
    assert Settings(anthropic_api_key="x").query_classifier_model == "claude-haiku-4-5"
