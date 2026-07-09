# tests/dashboard/test_goals_status_route.py
from contextlib import contextmanager

import app as appmod


class _FakeCursor:
    def __init__(self, rowcount): self.rowcount = rowcount; self.executed = []
    def execute(self, sql, params=None): self.executed.append((sql, params))


class _FakeStore:
    def __init__(self, rowcount=1): self._rowcount = rowcount; self.last = None
    @contextmanager
    def cursor(self):
        self.last = _FakeCursor(self._rowcount)
        yield self.last


def _client(monkeypatch, store):
    monkeypatch.setattr(appmod, "_ready", True)
    monkeypatch.setattr(appmod, "_sql_store", store)
    appmod._dashboard_cache["payload"] = {"stale": True}
    return appmod.app.test_client()


def test_sets_status_and_busts_cache(monkeypatch):
    store = _FakeStore(rowcount=1)
    client = _client(monkeypatch, store)
    r = client.post("/goals/42/status", json={"status": "in_progress"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok" and body["user_status"] == "in_progress" and body["id"] == 42
    # UPDATE issued with the value + id, and the dashboard cache was invalidated
    sql, params = store.last.executed[0]
    assert "UPDATE goals SET user_status" in sql and params == ("in_progress", 42)
    assert appmod._dashboard_cache["payload"] is None


def test_empty_status_clears_to_null(monkeypatch):
    store = _FakeStore(rowcount=1)
    client = _client(monkeypatch, store)
    r = client.post("/goals/7/status", json={"status": ""})
    assert r.status_code == 200 and r.get_json()["user_status"] is None
    assert store.last.executed[0][1] == (None, 7)


def test_invalid_status_rejected(monkeypatch):
    client = _client(monkeypatch, _FakeStore())
    r = client.post("/goals/1/status", json={"status": "banana"})
    assert r.status_code == 400


def test_unknown_goal_returns_404(monkeypatch):
    client = _client(monkeypatch, _FakeStore(rowcount=0))
    r = client.post("/goals/999/status", json={"status": "completed"})
    assert r.status_code == 404


def test_not_ready_returns_503(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", False)
    r = appmod.app.test_client().post("/goals/1/status", json={"status": "completed"})
    assert r.status_code == 503
