# tests/dashboard/test_projects.py
import datetime
from contextlib import contextmanager

from src.dashboard.projects import (
    Projects, classify_resolution,
    normalize_grant_status, normalize_resolution_status,
)


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


def test_classify_resolution_by_title():
    assert classify_resolution("A Resolution approving the Preliminary/Final Land Development") == "land_development"
    assert classify_resolution("A Resolution authorizing the submission of a grant application") == "grant_action"
    assert classify_resolution("A Resolution authorizing a professional services agreement") == "contract"
    assert classify_resolution("A Resolution approving the First Proposed 2026 Budget") == "budget"
    assert classify_resolution("A Resolution reappointing a board member") == "appointment"
    assert classify_resolution("A Resolution honoring the retiring chief") == "other"
    assert classify_resolution("") == "other"


def test_normalize_statuses():
    assert normalize_grant_status("active") == "Active"
    assert normalize_grant_status("AWARDED") == "Awarded"
    assert normalize_grant_status("applied") == "Proposed"
    assert normalize_grant_status("pending") == "Proposed"
    assert normalize_grant_status("closed") == "Closed"
    assert normalize_grant_status("weird") == "Active"          # default
    assert normalize_resolution_status("Passed") == "Active"
    assert normalize_resolution_status("Tabled") == "Stalled"
    assert normalize_resolution_status("Failed") == "Closed"
    assert normalize_resolution_status("") == "Proposed"        # default


def test_build_assembles_typed_projects_and_buckets_administrative():
    now = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "TASA Grant (Walnut St.)", "department": "Public Works",
             "amount": 1000000.0, "start_date": datetime.date(2025, 1, 1),
             "end_date": datetime.date(2026, 5, 1), "status": "awarded", "source_file": "g.pdf"},
            {"id": 2, "grant_name": "", "department": "X", "amount": None,
             "start_date": None, "end_date": None, "status": "active", "source_file": None},  # blank title -> skipped
        ],
        "FROM resolutions": [
            {"resolution_number": "5-2026", "title": "A Resolution approving a Land Development plan",
             "vendor": "Pennmark", "amount": None, "department": "Bureau of Planning",
             "adopted_date": datetime.date(2026, 1, 15), "status": "Passed", "source_file": "r5.pdf"},
            {"resolution_number": "3-2026", "title": "A Resolution approving the 2026 Budget",
             "vendor": "", "amount": None, "department": "Finance",
             "adopted_date": datetime.date(2026, 1, 15), "status": "Passed", "source_file": "r3.pdf"},
        ],
    })
    out = Projects(store, now=now).build()
    assert set(out) == {"projects", "administrative", "counts", "funding_in_flight"}
    # blank-title grant dropped; land-development resolution kept; budget bucketed
    titles = [p["title"] for p in out["projects"]]
    assert "TASA Grant (Walnut St.)" in titles
    assert any(p["type"] == "land_development" for p in out["projects"])
    assert len(out["administrative"]) == 1 and out["administrative"][0]["type"] == "budget"
    # types + status normalization surfaced
    grant = next(p for p in out["projects"] if p["source"] == "grant")
    assert grant["type"] == "grant" and grant["status"] == "Awarded"
    assert grant["id"] == "grant-1" and grant["end_date"] == "2026-05-01"
    res = next(p for p in out["projects"] if p["source"] == "resolution")
    assert res["id"] == "res-5-2026" and res["party"] == "Pennmark" and res["status"] == "Active"
    # funding_in_flight sums active/awarded/proposed project amounts
    assert out["funding_in_flight"] == 1000000.0
    assert out["counts"]["by_type"]["grant"] == 1


def test_build_flags_expiring_grant_as_attention():
    now = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)  # today = 2026-03-01
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "Expiring soon", "department": "PW", "amount": 5.0,
             "start_date": None, "end_date": datetime.date(2026, 4, 1), "status": "active", "source_file": None},
            {"id": 2, "grant_name": "Far off", "department": "PW", "amount": 5.0,
             "start_date": None, "end_date": datetime.date(2027, 1, 1), "status": "active", "source_file": None},
        ],
        "FROM resolutions": [],
    })
    out = Projects(store, now=now).build()
    a = {p["title"]: p["attention"] for p in out["projects"]}
    assert a["Expiring soon"] is True and a["Far off"] is False
    assert out["counts"]["attention"] == 1


def test_build_empty_tables():
    store = _FakeStore({"FROM grants": [], "FROM resolutions": []})
    out = Projects(store).build()
    assert out == {"projects": [], "administrative": [],
                   "counts": {"active": 0, "attention": 0, "by_type": {}},
                   "funding_in_flight": 0}
