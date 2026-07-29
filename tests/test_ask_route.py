# tests/test_ask_route.py
import app as appmod
from src.models import QueryResponse


class _RecordingPipeline:
    def __init__(self):
        self.calls = []

    def ask(self, question, history=None):
        self.calls.append({"question": question, "history": history})
        return QueryResponse(question=question, answer="ok")


def _client(monkeypatch, pipeline):
    monkeypatch.setattr(appmod, "_ready", True)
    monkeypatch.setattr(appmod, "_pipeline", pipeline)
    # Disable eval sampling so the route stays deterministic.
    monkeypatch.setattr(appmod, "get_settings", lambda: type("C", (), {"eval_sample_rate": 0.0})())
    return appmod.app.test_client()


def test_ask_without_history_passes_none(monkeypatch):
    pipe = _RecordingPipeline()
    client = _client(monkeypatch, pipe)
    r = client.post("/ask", json={"question": "who directs public works?"})
    assert r.status_code == 200
    assert pipe.calls[0]["question"] == "who directs public works?"
    assert pipe.calls[0]["history"] is None


def test_ask_forwards_history(monkeypatch):
    pipe = _RecordingPipeline()
    client = _client(monkeypatch, pipe)
    r = client.post("/ask", json={
        "question": "what about 2023?",
        "history": [{"question": "fire dept budget 2024?", "answer": "$5M"}],
    })
    assert r.status_code == 200
    assert pipe.calls[0]["history"] == [
        {"question": "fire dept budget 2024?", "answer": "$5M"}
    ]


def test_ask_sanitizes_non_list_history(monkeypatch):
    pipe = _RecordingPipeline()
    client = _client(monkeypatch, pipe)
    r = client.post("/ask", json={"question": "q", "history": "not-a-list"})
    assert r.status_code == 200
    assert pipe.calls[0]["history"] is None


def test_ask_sanitizes_malformed_history_entries(monkeypatch):
    pipe = _RecordingPipeline()
    client = _client(monkeypatch, pipe)
    r = client.post("/ask", json={
        "question": "q",
        "history": ["junk", {"question": "prior?", "answer": "yes"}, 42],
    })
    assert r.status_code == 200
    # Only the well-formed dict survives, coerced to {question, answer} strings.
    assert pipe.calls[0]["history"] == [{"question": "prior?", "answer": "yes"}]
