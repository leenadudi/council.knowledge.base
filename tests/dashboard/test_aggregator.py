# tests/dashboard/test_aggregator.py
import datetime
import decimal
from contextlib import contextmanager
from src.dashboard.aggregator import DashboardAggregator, quarter_start


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


def test_quarter_start_mapping():
    assert quarter_start(2026, "Q1") == datetime.date(2026, 1, 1)
    assert quarter_start(2026, "Q3") == datetime.date(2026, 7, 1)
    assert quarter_start(2026, "Q9") == datetime.date(2026, 1, 1)  # unknown → Jan


def test_kpis_shape_and_coverage():
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "FROM grants": [{"active": 12, "expiring": 3}],
        "FROM expenditures": [{"ytd": 4200000.0, "budget": 9000000.0}],
        "MAX(year)": [{"year": 2026}],
        "MAX(quarter)": [{"quarter": "Q1"}],
        "coverage_filed": [{"filed": 8}],
        "coverage_total": [{"total": 14}],
        "FROM resolutions": [{"c": 0}],
        "document_type='unclassified'": [{"c": 1}],
    })
    kpis = DashboardAggregator(store, now=now)._build_kpis()
    assert kpis["active_grants"] == 12
    assert kpis["grants_expiring_soon"] == 3
    assert kpis["ytd_spend"] == 4200000.0
    assert kpis["revised_budget"] == 9000000.0
    assert kpis["latest_period"] == {"year": 2026, "quarter": "Q1"}
    assert kpis["report_coverage"] == {"filed": 8, "total_departments": 14}
    assert kpis["resolutions_count"] == 0
    assert kpis["unclassified_docs"] == 1


def test_timeline_shapes_dates_and_handles_empty_resolutions():
    store = _FakeStore({
        "FROM grants": [
            {"id": 1, "grant_name": "NEHA-FDA", "department": "Health Office",
             "start_date": datetime.date(2025, 1, 1), "end_date": datetime.date(2026, 1, 1),
             "status": "active", "amount": decimal.Decimal("14000.00")},
            {"id": 2, "grant_name": "EPA-Water", "department": "Public Works",
             "start_date": datetime.date(2025, 3, 15), "end_date": None,
             "status": "pending", "amount": 5000.0},
        ],
        "FROM documents": [
            {"id": 10, "department": "Codes", "quarter": "Q2", "year": 2025, "document_type": "quarterly_report"},
        ],
        "FROM resolutions": [],  # empty today
        "GROUP BY year, quarter": [
            {"year": 2025, "quarter": "Q1", "ytd": 100000.0},
            {"year": 2025, "quarter": "Q2", "ytd": 150000.0},
        ],
    })
    tl = DashboardAggregator(store)._build_timeline()
    # Fix 1/2: explicit dates pass through iso() correctly
    assert tl["grants"][0]["start"] == "2025-01-01" and tl["grants"][0]["end"] == "2026-01-01"
    # Fix 5: Decimal amount is coerced to float
    assert isinstance(tl["grants"][0]["amount"], float) and tl["grants"][0]["amount"] == 14000.0
    # Fix 2/3: null end_date falls back to start + 1 year
    assert tl["grants"][1]["end"] == "2026-03-15"
    assert tl["reports"][0]["date"] == "2025-04-01"  # Q2 → Apr 1
    assert tl["resolutions"] == []
    assert tl["spending"][1]["period"] == "2025 Q2" and tl["spending"][1]["ytd_expended"] == 150000.0


def test_timeline_leap_day_safe_end_date():
    """Fix 3: start_date of Feb 29 (leap year) must not crash when deriving +1yr end."""
    store = _FakeStore({
        "FROM grants": [
            {"id": 3, "grant_name": "Leap-Grant", "department": "Admin",
             "start_date": datetime.date(2024, 2, 29), "end_date": None,
             "status": "active", "amount": 1000.0},
        ],
        "FROM documents": [],
        "FROM resolutions": [],
        "GROUP BY year, quarter": [],
    })
    tl = DashboardAggregator(store)._build_timeline()
    # 2025-02-29 doesn't exist; should fall back to 2025-02-28
    assert tl["grants"][0]["end"] == "2025-02-28"


def test_build_assembles_happy_path_payload():
    store = _FakeStore({
        "FROM grants WHERE (LOWER(status)": [{"active": 0, "expiring": 0}],
        "SUM(amount) AS funds": [{"funds": 0.0}],
        "FROM expenditures WHERE line_item": [{"ytd": 0, "budget": 0}],
        "MAX(year)": [{"year": None}],
        "COUNT(*) AS c FROM resolutions": [{"c": 0}],
        "document_type='unclassified'": [{"c": 0}],
        # timeline queries
        "FROM grants WHERE start_date": [],
        "FROM documents\n": [],
        "FROM resolutions WHERE adopted_date": [],
        "GROUP BY year, quarter": [],
        # tables
        "FROM grants ORDER BY start_date": [],
        "GROUP BY department ORDER BY ytd_expended": [{"department": "Codes", "revised_budget": 1.0, "ytd_expended": 0.5}],
        "FROM documents ORDER BY": [],
    })
    out = DashboardAggregator(store).build()
    assert set(out) >= {"generated_at", "kpis", "timeline", "tables"}
    assert out["tables"]["spending_by_dept"][0]["department"] == "Codes"


def test_build_votes_rollcall_and_member_records():
    import decimal
    # rows come pre-joined to resolutions (title/amount/status repeat per resolution)
    store = _FakeStore({"FROM votes": [
        {"resolution_number": "9-2026", "council_member": "Wanda Williams", "vote": "Yea",
         "title": "BUILD Grant", "amount": decimal.Decimal("3000000"), "status": "Passed"},
        {"resolution_number": "9-2026", "council_member": "Danielle Bowers", "vote": "Nay",
         "title": "BUILD Grant", "amount": decimal.Decimal("3000000"), "status": "Passed"},
        {"resolution_number": "9-2026", "council_member": "Ralph Rodriguez", "vote": "abstain",
         "title": "BUILD Grant", "amount": decimal.Decimal("3000000"), "status": "Passed"},
        {"resolution_number": "10-2026", "council_member": "Wanda Williams", "vote": "YES",
         "title": None, "amount": None, "status": None},
        {"resolution_number": "10-2026", "council_member": "Danielle Bowers", "vote": "absent",
         "title": None, "amount": None, "status": None},
    ]})
    v = DashboardAggregator(store)._build_votes()

    r9 = next(r for r in v["by_resolution"] if r["resolution_number"] == "9-2026")
    assert r9["tally"] == {"yea": 1, "nay": 1, "abstain": 1, "absent": 0, "other": 0}
    assert r9["title"] == "BUILD Grant" and r9["amount"] == 3000000.0 and r9["status"] == "Passed"
    assert {x["member"] for x in r9["votes"]} == {"Wanda Williams", "Danielle Bowers", "Ralph Rodriguez"}

    williams = next(m for m in v["by_member"] if m["member"] == "Wanda Williams")
    assert williams["total"] == 2 and williams["yea"] == 2   # "Yea" and "YES" both bucket to yea
    assert v["by_member"][0]["total"] >= v["by_member"][-1]["total"]


class _BoomStore:
    from contextlib import contextmanager as _cm
    @_cm
    def cursor(self):
        raise RuntimeError("db down")
        yield  # pragma: no cover


def test_build_never_raises_records_errors():
    out = DashboardAggregator(_BoomStore()).build()
    assert out["kpis"] is None and out["timeline"] is None and out["tables"] is None
    assert out["departments"] is None and out["resolutions"] is None
    assert set(out["errors"].keys()) == {
        "kpis", "timeline", "tables", "departments", "resolutions",
        "goals", "legislation", "meetings", "budget", "vacancies",
    }


def test_build_departments_shape():
    store = _FakeStore({
        "document_type = ANY": [
            {"department": "Codes", "document_type": "quarterly_report"},
            {"department": "Fire", "document_type": "budget"},
        ],
        "AS rb": [{"department": "Codes", "rb": 100.0, "ytd": 40.0}],
    })
    depts = DashboardAggregator(store)._build_departments()
    codes = next(d for d in depts if d["department"] == "Codes")
    assert codes["revised_budget"] == 100.0 and codes["ytd_expended"] == 40.0 and codes["report_count"] == 1
    fire = next(d for d in depts if d["department"] == "Fire")
    assert fire["revised_budget"] == 0 and fire["report_count"] == 0   # budget doc, not a quarterly_report


def test_build_resolutions_shape():
    import datetime
    store = _FakeStore({"FROM resolutions ORDER BY": [
        {"resolution_number": "9-2026", "title": "BUILD Grant", "status": "Passed",
         "amount": 3000000.0, "vendor": "USDOT", "adopted_date": datetime.date(2026,1,27)}]})
    r = DashboardAggregator(store)._build_resolutions()[0]
    assert r["resolution_number"] == "9-2026" and r["amount"] == 3000000.0
    assert r["adopted_date"] == "2026-01-27"    # ISO string


def test_build_resolutions_none_passthrough():
    """amount=None and adopted_date=None must pass through as None (UI renders as '—')."""
    store = _FakeStore({"FROM resolutions ORDER BY": [
        {"resolution_number": "10-2026", "title": "Pending Resolution", "status": "Pending",
         "amount": None, "vendor": None, "adopted_date": None}]})
    r = DashboardAggregator(store)._build_resolutions()[0]
    assert r["amount"] is None, "None amount must not be coerced to 0.0"
    assert r["adopted_date"] is None, "None adopted_date must not be coerced"


def test_build_includes_new_panels():
    store = _FakeStore({
        "FILTER (WHERE": [{"active": 0, "expiring": 0}],
        "SUM(amount)": [{"funds": 500000.0}],
        "FROM expenditures": [{"ytd": 0, "budget": 0}],
        "MAX(year)": [{"year": None}], "FROM resolutions": [],
        "document_type='unclassified'": [{"c": 0}],
        "DISTINCT department": [], "GROUP BY department": [], "quarterly_report' GROUP BY": [],
        "GROUP BY year, quarter": [], "FROM documents ORDER BY": [],
    })
    out = DashboardAggregator(store).build()
    assert "departments" in out and "resolutions" in out
    assert out["kpis"]["grant_funds_active"] == 500000.0
