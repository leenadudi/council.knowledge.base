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


def test_dashboard_redirects_to_home(monkeypatch):
    # The standalone /dashboard page was unified into "/"; it now redirects.
    monkeypatch.setattr(appmod, "_ready", True)
    r = _client().get("/dashboard")
    assert r.status_code in (301, 302)
    assert r.headers["Location"].endswith("/")


def test_home_serves_redesign_with_nav_tabs():
    # "/" serves the redesign shell with its explore nav tabs.
    html = _client().get("/").data.decode()
    for tab in ("overview", "activity", "money", "goals", "people"):
        assert f'data-tab="{tab}"' in html
    # no external chart libraries (CSP-safe, inline SVG only)
    assert "chart.js" not in html.lower() and "vis-timeline" not in html.lower()
