import app as A


def test_proposals_route_returns_pending(monkeypatch):
    class _Store:
        def get_pending_type_proposals(self):
            return [{"id": 1, "source_file": "b.pdf", "proposed_type": "boards",
                     "payload": {"record_types": [{"name": "board_member"}]},
                     "created_at": "2026-07-14T00:00:00"}]
    monkeypatch.setattr(A, "_sql_store", _Store())
    client = A.app.test_client()
    resp = client.get("/proposals")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data[0]["proposed_type"] == "boards"


def test_proposals_route_503_when_store_unset(monkeypatch):
    monkeypatch.setattr(A, "_sql_store", None)
    resp = A.app.test_client().get("/proposals")
    assert resp.status_code == 503
