import app as appmod


def _client():
    return appmod.app.test_client()


def test_dashboard_data_ready(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", True)
    monkeypatch.setattr(appmod, "_sql_store", object())
    class _Agg:
        def __init__(self, store): pass
        def build(self): return {"generated_at": "t", "kpis": {}, "timeline": {}, "tables": {}}
    monkeypatch.setattr(appmod, "DashboardAggregator", _Agg, raising=False)
    r = _client().get("/dashboard/data")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) >= {"generated_at", "kpis", "timeline", "tables"}


def test_dashboard_data_not_ready(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", False)
    r = _client().get("/dashboard/data")
    assert r.status_code == 503


def test_dashboard_page_renders(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", True)
    r = _client().get("/dashboard")
    assert r.status_code == 200
    assert b"dashboard" in r.data.lower()


def test_dashboard_has_all_tabs(monkeypatch):
    monkeypatch.setattr(appmod, "_ready", True)
    html = _client().get("/dashboard").data.decode()
    for view in ("view-dashboard","view-departments","view-grants","view-resolutions",
                 "view-goals","view-projects","view-boards"):
        assert f'id="{view}"' in html
    # empty-states present for data-less tabs
    assert "no goals tracked yet" in html.lower()
    # no chart libraries
    assert "vis-timeline" not in html.lower() and "chart.js" not in html.lower() and "chart.umd" not in html.lower()
    # ask/documents link to main app
    assert 'href="/"' in html
