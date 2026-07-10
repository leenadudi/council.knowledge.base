import datetime
from contextlib import contextmanager

from src.dashboard.projects import Projects, _normalize_party


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


def test_normalize_party_collapses_variants():
    a = _normalize_party("Lamar Advantage GP Company, LLC")
    b = _normalize_party("LAMAR ADVANTAGE GP COMPANY LLC")
    c = _normalize_party("  Lamar Advantage GP Company , llc. ")
    assert a == b == c and a != ""


def _lamar(rn, loc):
    return {"resolution_number": rn,
            "title": f"A Resolution authorizing a lease agreement for a billboard {loc}",
            "vendor": "Lamar Advantage GP Company, LLC", "amount": 13000.0,
            "department": "Mayor", "adopted_date": datetime.date(2026, 2, 1), "status": "Passed",
            "source_file": f"{rn}.pdf"}


def test_build_assigns_shared_group_key_to_siblings():
    now = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "BUILD Grant", "department": "Public Works", "amount": 5.0,
             "start_date": None, "end_date": None, "status": "awarded", "source_file": "g.pdf"},
        ],
        "FROM resolutions": [
            _lamar("10-2026", "on I-83"),
            _lamar("11-2026", "on Paxton St"),
            {"resolution_number": "22-2026",
             "title": "A Resolution authorizing a professional services agreement",
             "vendor": "McCormick Law Firm", "amount": None, "department": "Mayor",
             "adopted_date": datetime.date(2026, 3, 2), "status": "Passed", "source_file": "22.pdf"},
        ],
    })
    out = Projects(store, now=now).build()
    by_num = {p["resolution_number"]: p for p in out["projects"] if p.get("resolution_number")}
    # the two Lamar leases share one key
    assert by_num["10-2026"]["group_key"] == by_num["11-2026"]["group_key"]
    # a different vendor differs
    assert by_num["22-2026"]["group_key"] != by_num["10-2026"]["group_key"]
    # grants are never grouped
    grant = next(p for p in out["projects"] if p["source"] == "grant")
    assert grant["group_key"] is None
