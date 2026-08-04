# tests/dashboard/test_goals_trackability_route.py
from contextlib import contextmanager

import app as appmod

_GOAL_ROWS = [
    {"id": 1, "department": "Bureau of Police", "goal_title": "Reduce gun violence"},
    {"id": 2, "department": "Bureau of Police", "goal_title": "Reduce  gun violence "},
    {"id": 3, "department": "Bureau of Police", "goal_title": "Another goal"},
    {"id": 4, "department": "Bureau of Fire", "goal_title": "Reduce gun violence"},
]


class _FakeCursor:
    """Scripted cursor: first SELECT → the clicked row, second → all goals."""
    def __init__(self, row):
        self._row = row
        self.executed = []
        self._selects = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row

    def fetchall(self):
        return _GOAL_ROWS


class _FakeStore:
    def __init__(self, row):
        self._row = row
        self.last = None

    @contextmanager
    def cursor(self):
        self.last = _FakeCursor(self._row)
        yield self.last


def _client(monkeypatch, store):
    monkeypatch.setattr(appmod, "_ready", True)
    monkeypatch.setattr(appmod, "_sql_store", store)
    appmod._dashboard_cache["payload"] = {"stale": True}
    return appmod.app.test_client()


def test_label_applies_to_whole_logical_goal(monkeypatch):
    store = _FakeStore(_GOAL_ROWS[0])
    client = _client(monkeypatch, store)
    r = client.post("/goals/1/trackability", json={"label": "measurable"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["trackability"] == "measurable"
    # ids 1 and 2 share the logical goal (whitespace-variant title, same dept);
    # 3 (other title) and 4 (other dept) do not
    sql, params = store.last.executed[-1]
    assert "trackability_source='human'" in sql
    assert sorted(params[-1]) == [1, 2]
    assert body["rows_updated"] == 2
    assert appmod._dashboard_cache["payload"] is None


def test_empty_label_clears_all_trackability_fields(monkeypatch):
    store = _FakeStore(_GOAL_ROWS[0])
    client = _client(monkeypatch, store)
    r = client.post("/goals/1/trackability", json={"label": ""})
    assert r.status_code == 200 and r.get_json()["trackability"] is None
    sql, _ = store.last.executed[-1]
    assert "trackability=NULL" in sql and "trackability_source=NULL" in sql


def test_invalid_label_rejected(monkeypatch):
    client = _client(monkeypatch, _FakeStore(_GOAL_ROWS[0]))
    r = client.post("/goals/1/trackability", json={"label": "needs_review"})
    assert r.status_code == 400  # triage state is machine-set, not a clerk choice


def test_unknown_goal_returns_404(monkeypatch):
    client = _client(monkeypatch, _FakeStore(row=None))
    r = client.post("/goals/999/trackability", json={"label": "milestone"})
    assert r.status_code == 404


def test_not_ready_returns_503(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", False)
    r = appmod.app.test_client().post("/goals/1/trackability", json={"label": "milestone"})
    assert r.status_code == 503
