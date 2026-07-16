import app as A
from src.ingestion import approval


def test_approve_route_success(monkeypatch):
    monkeypatch.setattr(A, "_sql_store", object())
    monkeypatch.setattr(approval, "approve_proposal",
                        lambda store, pid: {"type_name": "boards", "created_tables": ["board_members"]})
    resp = A.app.test_client().post("/proposals/7/approve")
    assert resp.status_code == 200
    assert resp.get_json()["created_tables"] == ["board_members"]


def test_approve_route_approval_error_is_400(monkeypatch):
    monkeypatch.setattr(A, "_sql_store", object())
    def boom(store, pid): raise approval.ApprovalError("already approved")
    monkeypatch.setattr(approval, "approve_proposal", boom)
    resp = A.app.test_client().post("/proposals/7/approve")
    assert resp.status_code == 400 and "already approved" in resp.get_json()["error"]


def test_reject_route_success(monkeypatch):
    monkeypatch.setattr(A, "_sql_store", object())
    monkeypatch.setattr(approval, "reject_proposal",
                        lambda store, pid, note="": {"proposal_id": pid, "status": "rejected"})
    resp = A.app.test_client().post("/proposals/7/reject")
    assert resp.status_code == 200 and resp.get_json()["status"] == "rejected"


def test_approve_route_503_when_store_unset(monkeypatch):
    monkeypatch.setattr(A, "_sql_store", None)
    assert A.app.test_client().post("/proposals/7/approve").status_code == 503
