# tests/dashboard/test_questions_route.py
from contextlib import contextmanager

import app as appmod
import src.dashboard.review_questions as rqmod


class _FakeCursor:
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


def _store_with_one_finding():
    # a single latest-period goal with a target and no status -> one no_progress finding
    return _FakeStore({
        "FROM goals": [{"id": 1, "department": "Bureau of Fire", "year": 2026, "quarter": "Q1",
                        "goal_title": "Reduce response time", "description": "",
                        "target": "< 6 min", "status": None}],
        "LIMIT 1": [], "GROUP BY department": [],
    })


def _setup(monkeypatch, store):
    monkeypatch.setattr(appmod, "_ready", True)
    monkeypatch.setattr(appmod, "_sql_store", store)
    appmod._questions_cache.clear()
    return appmod.app.test_client()


def test_polishes_once_then_serves_from_cache(monkeypatch):
    calls = {"n": 0}
    def fake_phrase(questions, settings, client=None):
        calls["n"] += 1
        return [q + " [polished]" for q in questions]
    monkeypatch.setattr(rqmod, "phrase_questions", fake_phrase)

    client = _setup(monkeypatch, _store_with_one_finding())

    r1 = client.get("/questions/Bureau of Fire")
    assert r1.status_code == 200
    body = r1.get_json()
    assert body["polished"] is True
    assert body["questions"][0]["question"].endswith("[polished]")
    assert body["questions"][0]["signal"] == "goal_no_progress"
    assert calls["n"] == 1

    # identical request -> cache hit, no second LLM call
    r2 = client.get("/questions/Bureau of Fire")
    assert r2.get_json()["polished"] is True
    assert calls["n"] == 1


def test_falls_back_to_templated_on_phrasing_error(monkeypatch):
    def boom(questions, settings, client=None):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(rqmod, "phrase_questions", boom)

    client = _setup(monkeypatch, _store_with_one_finding())
    body = client.get("/questions/Bureau of Fire").get_json()
    assert body["polished"] is False
    # templated wording preserved, still grounded in the real target
    assert "< 6 min" in body["questions"][0]["question"]


def test_unknown_department_returns_empty(monkeypatch):
    monkeypatch.setattr(rqmod, "phrase_questions", lambda *a, **k: [])
    client = _setup(monkeypatch, _store_with_one_finding())
    body = client.get("/questions/Nonexistent Dept").get_json()
    assert body["questions"] == [] and body["polished"] is False


def test_not_ready_returns_503(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", False)
    r = appmod.app.test_client().get("/questions/Bureau of Fire")
    assert r.status_code == 503
