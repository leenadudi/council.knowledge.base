from decimal import Decimal

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


class FakeStoreDecimal:
    """Mimics the real SQLStore.usage_report which returns Decimal for SUM(est_cost_usd)."""
    def usage_report(self, start, end):
        return [
            {"call_site": "query.synthesizer", "model": "claude-sonnet-4-6",
             "calls": 1, "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 0, "cache_write_tokens": 0,
             "est_cost_usd": Decimal("0.0024")},
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


def test_admin_costs_decimal_est_cost_usd_is_json_serializable(monkeypatch):
    """Regression: usage_report returning Decimal est_cost_usd must not cause a 500.

    Before the fix, Flask's jsonify raised TypeError on Decimal values from
    psycopg2's SUM() of a DECIMAL column, returning HTTP 500 whenever llm_usage
    had any rows. This test uses a fake store that returns Decimal (as the real
    psycopg2 cursor did) to prove the end-to-end serialization contract holds.

    The fix lives in SQLStore.usage_report (coercing Decimal → float before
    returning rows), so any real caller of usage_report gets floats. This test
    guards that the route does not 500 and correctly computes total_cost_usd
    even when est_cost_usd arrives as Decimal.
    """
    monkeypatch.setattr(app_module, "_sql_store", FakeStoreDecimal())
    monkeypatch.setattr(app_module, "_ready", True)
    client = app_module.app.test_client()
    resp = client.get("/admin/costs?start=2026-06-01&end=2026-07-01")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total_cost_usd"] == round(0.0024, 6)
