import app as app_module


class FakeStore:
    def usage_report(self, start, end):
        return [
            {"call_site": "query.synthesizer", "model": "claude-sonnet-4-6",
             "calls": 3, "input_tokens": 300, "output_tokens": 90,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "est_cost_usd": 0.0024},
            {"call_site": "query.classifier", "model": "claude-sonnet-4-6",
             "calls": 3, "input_tokens": 120, "output_tokens": 30,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "est_cost_usd": 0.0008},
        ]


def test_admin_costs_returns_breakdown(monkeypatch):
    monkeypatch.setattr(app_module, "_sql_store", FakeStore())
    monkeypatch.setattr(app_module, "_ready", True)
    client = app_module.app.test_client()
    resp = client.get("/admin/costs?start=2026-06-01&end=2026-07-01")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["start"] == "2026-06-01" and body["end"] == "2026-07-01"
    assert len(body["by_call_site"]) == 2
    assert body["total_cost_usd"] == round(0.0024 + 0.0008, 6)
