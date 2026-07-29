# tests/dashboard/test_ytd.py
import datetime
from contextlib import contextmanager

from src.dashboard.ytd import YTDReport


class _FakeCursor:
    """Returns canned rows based on a substring match of the executed SQL."""
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


_NOW = datetime.datetime(2026, 7, 29, tzinfo=datetime.timezone.utc)


def _store(**overrides):
    """A populated store covering 2025 Q3-Q4 and 2026 Q1-Q2."""
    responses = {
        # per-(year, quarter, department) expenditure snapshots; ytd_expended is
        # cumulative within a year, so 2026 Q2 already contains Q1.
        "FROM expenditures": [
            {"year": 2025, "quarter": "Q4", "department": "Bureau of Fire", "ytd": 900.0, "rb": 1000.0},
            {"year": 2026, "quarter": "Q1", "department": "Bureau of Fire", "ytd": 250.0, "rb": 1200.0},
            {"year": 2026, "quarter": "Q1", "department": "Police", "ytd": 100.0, "rb": 800.0},
            {"year": 2026, "quarter": "Q2", "department": "Bureau of Fire", "ytd": 600.0, "rb": 1200.0},
            {"year": 2026, "quarter": "Q2", "department": "Police", "ytd": 300.0, "rb": 800.0},
        ],
        "FROM resolutions": [
            {"resolution_number": "1-2026", "amount": 500.0, "adopted_date": datetime.date(2026, 1, 15)},
            {"resolution_number": "2-2026", "amount": None, "adopted_date": datetime.date(2026, 4, 2)},
            {"resolution_number": "9-2025", "amount": 50.0, "adopted_date": datetime.date(2025, 11, 3)},
            {"resolution_number": "??", "amount": 25.0, "adopted_date": None},  # undated -> excluded
        ],
        "FROM legislation": [
            {"bill_number": "3-2026", "amount": None, "adopted_date": datetime.date(2026, 2, 10)},
            {"bill_number": "4-2026", "amount": None, "adopted_date": None},    # undated -> excluded
        ],
        "FROM meetings": [
            {"meeting_date": datetime.date(2026, 1, 8)},
            {"meeting_date": datetime.date(2026, 1, 22)},
            {"meeting_date": datetime.date(2026, 4, 9)},
            {"meeting_date": datetime.date(2025, 12, 2)},
        ],
        "FROM grants": [
            # duplicate rows for the same grant must collapse before counting
            {"id": 1, "grant_name": "Safe Streets", "department": "Police", "amount": 700.0,
             "start_date": datetime.date(2026, 2, 1), "end_date": datetime.date(2027, 2, 1), "status": "active"},
            {"id": 2, "grant_name": "safe streets", "department": "Police", "amount": 700.0,
             "start_date": datetime.date(2026, 2, 1), "end_date": datetime.date(2027, 2, 1), "status": "active"},
            # started previous year, still live -> active but not "new" in 2026
            {"id": 3, "grant_name": "CDBG", "department": "Budget & Finance", "amount": 900.0,
             "start_date": datetime.date(2025, 7, 1), "end_date": datetime.date(2026, 12, 31), "status": "awarded"},
            # ended long ago -> neither new nor active
            {"id": 4, "grant_name": "Old Grant", "department": "Parks", "amount": 10.0,
             "start_date": datetime.date(2023, 1, 1), "end_date": datetime.date(2024, 1, 1), "status": "closed"},
        ],
        "FROM goals": [
            {"department": "Bureau of Fire", "year": 2026, "status": "Completed in March", "user_status": None},
            {"department": "Bureau of Fire", "year": 2026, "status": "ongoing", "user_status": None},
            {"department": "Police", "year": 2026, "status": "", "user_status": None},          # not started
            {"department": "Police", "year": 2026, "status": "ongoing", "user_status": "completed"},  # override wins
            {"department": "Parks", "year": 2025, "status": "", "user_status": None},           # other year
        ],
    }
    responses.update(overrides)
    return _FakeStore(responses)


def _report(store=None):
    return YTDReport(store or _store(), now=_NOW).build()


def test_years_listed_desc_and_reports_keyed_by_year():
    out = _report()
    assert out["years"] == [2026, 2025]
    assert set(out["reports"]) == {2026, 2025}


def test_spending_uses_latest_snapshot_not_sum():
    r = out = _report()["reports"][2026]
    # citywide YTD = Q2 snapshot (600+300), NOT Q1+Q2 summed
    assert r["spending"]["ytd_expended"] == 900.0
    assert r["spending"]["revised_budget"] == 2000.0
    assert r["spending"]["pct_used"] == 45
    # Q2 of 4 quarters elapsed -> expected 50%
    assert r["as_of_quarter"] == "Q2"
    assert r["quarters_elapsed"] == 2
    assert r["spending"]["expected_pct"] == 50


def test_spending_by_quarter_increments_with_filer_counts():
    sp = _report()["reports"][2026]["spending"]["by_quarter"]
    assert sp == [
        {"quarter": "Q1", "ytd_expended": 350.0, "increment": 350.0, "departments": 2},
        {"quarter": "Q2", "ytd_expended": 900.0, "increment": 550.0, "departments": 2},
    ]


def test_spending_by_department_latest_quarter_with_pct():
    depts = {d["department"]: d for d in _report()["reports"][2026]["spending"]["by_department"]}
    assert depts["Bureau of Fire"]["ytd_expended"] == 600.0
    assert depts["Bureau of Fire"]["revised_budget"] == 1200.0
    assert depts["Bureau of Fire"]["pct"] == 50
    assert depts["Bureau of Fire"]["as_of_quarter"] == "Q2"
    assert depts["Police"]["ytd_expended"] == 300.0


def test_department_missing_from_latest_quarter_keeps_its_last_snapshot():
    # Parks filed only Q1; its Q1 snapshot must still count toward the year total
    # instead of vanishing because the citywide latest quarter is Q2.
    store = _store(**{"FROM expenditures": [
        {"year": 2026, "quarter": "Q1", "department": "Bureau of Fire", "ytd": 100.0, "rb": 400.0},
        {"year": 2026, "quarter": "Q2", "department": "Bureau of Fire", "ytd": 250.0, "rb": 400.0},
        {"year": 2026, "quarter": "Q1", "department": "Parks", "ytd": 40.0, "rb": 100.0},
    ]})
    sp = _report(store)["reports"][2026]["spending"]
    assert sp["ytd_expended"] == 290.0 and sp["revised_budget"] == 500.0
    assert sp["pct_used"] == 58
    depts = {d["department"]: d for d in sp["by_department"]}
    assert depts["Parks"]["as_of_quarter"] == "Q1" and depts["Parks"]["ytd_expended"] == 40.0
    # uneven coverage is visible in the filer counts
    assert [q["departments"] for q in sp["by_quarter"]] == [2, 1]


def test_legislative_counts_bucket_by_adopted_year():
    leg = _report()["reports"][2026]["legislative"]
    assert leg["resolutions_adopted"] == 2
    assert leg["ordinances_adopted"] == 1
    assert leg["meetings_held"] == 3
    assert leg["council_authorized"] == 500.0
    assert leg["undated_items"] == 2  # one resolution + one bill without a date
    by_q = {q["quarter"]: q for q in leg["by_quarter"]}
    assert by_q["Q1"] == {"quarter": "Q1", "resolutions": 1, "ordinances": 1,
                          "meetings": 2, "authorized": 500.0}
    assert by_q["Q2"] == {"quarter": "Q2", "resolutions": 1, "ordinances": 0,
                          "meetings": 1, "authorized": 0.0}
    # prior year got its own report
    prev = _report()["reports"][2025]["legislative"]
    assert prev["resolutions_adopted"] == 1 and prev["meetings_held"] == 1


def test_grants_new_vs_active_deduped():
    g = _report()["reports"][2026]["grants"]
    assert g["new_count"] == 1                      # Safe Streets (dup collapsed)
    assert g["new_amount"] == 700.0
    assert g["active_count"] == 2                   # Safe Streets + CDBG live at _NOW
    assert [x["grant_name"] for x in g["new_grants"]] == ["Safe Streets"]
    assert g["new_grants"][0]["start_date"] == "2026-02-01"


def test_goals_bucketed_with_user_status_override():
    g = _report()["reports"][2026]["goals"]
    assert g == {"total": 4, "completed": 2, "in_progress": 1, "not_started": 1}


def test_empty_database_yields_no_years():
    out = YTDReport(_FakeStore({}), now=_NOW).build()
    assert out == {"years": [], "reports": {}}
