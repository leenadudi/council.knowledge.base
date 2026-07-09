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


def test_dept_key_merges_known_variants_without_over_merging():
    k = DashboardAggregator._dept_key
    assert k("Planning Bureau") == k("Bureau of Planning") == "planning"
    assert k("Harrisburg City Council") == k("City of Harrisburg City Council") == k("City Council") == "city council"
    assert k("Finance") == k("Department of Budget & Finance") == "budget & finance"
    assert k("Parks and Recreation") == k("Bureau of Parks & Recreation") == "parks & recreation"
    assert k("Park Maintenance") == "parks & recreation"
    assert k("Department of Economic Development & Building and Housing") == k("Department of Building & Housing Development") == "building & housing development"
    assert k("Codes/Health Department") == k("Bureau of Codes") == "codes"
    # must NOT over-merge distinct departments
    assert k("Bureau of Fire") == "fire" and k("Bureau of Police") == "police"
    # display prefers the name native to the key, not the aliased-in variant
    disp = DashboardAggregator._dept_display
    assert disp("codes", ["Codes/Health Department", "Bureau of Codes"]) == "Bureau of Codes"
    assert disp("city council", ["City of Harrisburg City Council", "City Council"]) == "City Council"


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
        # canonical coverage: roster = distinct dept-keys that ever filed a QR;
        # filed = distinct dept-keys filing in the latest period. Variants collapse.
        "coverage_rows": [
            {"department": "Bureau of Fire", "year": 2026, "quarter": "Q1"},
            {"department": "Parks & Recreation", "year": 2026, "quarter": "Q1"},
            {"department": "Bureau of Parks & Recreation", "year": 2026, "quarter": "Q1"},  # variant → merges with above
            {"department": "Bureau of Codes", "year": 2025, "quarter": "Q4"},  # roster but not latest period
        ],
        "FROM resolutions": [{"c": 0}],
        "document_type='unclassified'": [{"c": 1}],
    })
    kpis = DashboardAggregator(store, now=now)._build_kpis()
    assert kpis["active_grants"] == 12
    assert kpis["grants_expiring_soon"] == 3
    assert kpis["ytd_spend"] == 4200000.0
    assert kpis["revised_budget"] == 9000000.0
    assert kpis["latest_period"] == {"year": 2026, "quarter": "Q1"}
    # roster = {fire, parks & recreation, codes} = 3; filed Q1 2026 = {fire, parks & recreation} = 2
    assert kpis["report_coverage"] == {"filed": 2, "total_departments": 3}
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
    assert set(out) >= {"generated_at", "kpis", "timeline", "tables", "review_questions"}
    assert out["tables"]["spending_by_dept"][0]["department"] == "Codes"
    assert set(out["review_questions"]) == {"period", "departments"}


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
        {"resolution_number": "10-2026", "council_member": "Wanda Williams", "vote": "YEAS",
         "title": None, "amount": None, "status": None},   # plural form seen in real data
        {"resolution_number": "10-2026", "council_member": "Danielle Bowers", "vote": "absent",
         "title": None, "amount": None, "status": None},
    ]})
    v = DashboardAggregator(store)._build_votes()

    r9 = next(r for r in v["by_resolution"] if r["resolution_number"] == "9-2026")
    assert r9["tally"] == {"yea": 1, "nay": 1, "abstain": 1, "absent": 0, "other": 0}
    r10 = next(r for r in v["by_resolution"] if r["resolution_number"] == "10-2026")
    assert r10["tally"]["yea"] == 1 and r10["tally"]["absent"] == 1   # "YEAS" buckets to yea
    assert r9["title"] == "BUILD Grant" and r9["amount"] == 3000000.0 and r9["status"] == "Passed"
    assert {x["member"] for x in r9["votes"]} == {"Wanda Williams", "Danielle Bowers", "Ralph Rodriguez"}

    williams = next(m for m in v["by_member"] if m["member"] == "Wanda Williams")
    assert williams["total"] == 2 and williams["yea"] == 2   # "Yea" and "YES" both bucket to yea
    assert v["by_member"][0]["total"] >= v["by_member"][-1]["total"]


def test_build_metrics_keeps_latest_per_metric_and_groups_by_dept():
    import decimal
    store = _FakeStore({"FROM metrics": [
        {"department": "Fire", "metric_name": "Response time", "metric_value": decimal.Decimal("4.20"),
         "metric_unit": "min", "quarter": "Q2", "year": 2026},
        {"department": "Fire", "metric_name": "Response time", "metric_value": decimal.Decimal("5.10"),
         "metric_unit": "min", "quarter": "Q1", "year": 2026},   # older → dropped
        {"department": "Fire", "metric_name": "Calls", "metric_value": decimal.Decimal("1200"),
         "metric_unit": None, "quarter": "Q2", "year": 2026},
    ]})
    out = DashboardAggregator(store)._build_metrics()
    fire = next(d for d in out if d["department"] == "Fire")
    rt = next(m for m in fire["metrics"] if m["name"] == "Response time")
    assert rt["value"] == 4.20 and isinstance(rt["value"], float)
    assert {m["name"] for m in fire["metrics"]} == {"Response time", "Calls"}


def test_build_vendor_spend_aggregates_and_sorts():
    import decimal
    store = _FakeStore({"FROM resolutions\n            WHERE vendor": [
        {"vendor": "USDOT", "amount": decimal.Decimal("3000000"), "department": "Public Works"},
        {"vendor": "USDOT", "amount": decimal.Decimal("500000"), "department": "Engineering"},
        {"vendor": "Acme Paving", "amount": decimal.Decimal("120000"), "department": "Public Works"},
    ]})
    out = DashboardAggregator(store)._build_vendor_spend()
    assert out[0]["vendor"] == "USDOT"
    assert out[0]["total"] == 3500000.0 and out[0]["count"] == 2
    assert set(out[0]["departments"]) == {"Public Works", "Engineering"}


def test_build_commitments_authorized_actual_and_expiring():
    import datetime, decimal
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    store = _FakeStore({
        "GROUP BY department) AS authorized": [],  # unused key guard (query has ") AS authorized")
        ") AS authorized": [
            {"department": "Public Works", "authorized_total": decimal.Decimal("3500000")},
            {"department": "Bureau of Fire", "authorized_total": decimal.Decimal("100000")},
        ],
        "AS ytd_spend": [
            {"department": "Public Works", "ytd_spend": decimal.Decimal("1200000")},
            {"department": "Fire", "ytd_spend": decimal.Decimal("90000")},   # variant → merges with "Bureau of Fire"
        ],
        "FROM grants\n            WHERE end_date": [
            {"grant_name": "NEHA-FDA", "department": "Health", "end_date": datetime.date(2026, 8, 30),
             "amount": decimal.Decimal("14000")},
            {"grant_name": "Old-Grant", "department": "Admin", "end_date": datetime.date(2025, 1, 1),
             "amount": decimal.Decimal("1000")},   # already expired → excluded
        ],
    })
    out = DashboardAggregator(store, now=now)._build_commitments()
    pw = next(d for d in out["authorized_vs_spent"] if d["department"] == "Public Works")
    assert pw["authorized_total"] == 3500000.0 and pw["ytd_spend"] == 1200000.0
    fire = next(d for d in out["authorized_vs_spent"] if "Fire" in d["department"])
    assert fire["ytd_spend"] == 90000.0
    assert out["authorized_vs_spent"][0]["department"] == "Public Works"
    assert [g["grant_name"] for g in out["grants_expiring"]] == ["NEHA-FDA"]
    assert out["grants_expiring"][0]["days_left"] == 90


def test_build_vacancies_dedupes_across_quarters():
    store = _FakeStore({"FROM vacancies": [
        {"department": "Bureau of Police", "position_title": "Detective", "status": "open", "quarter": "Q3", "year": 2025},
        {"department": "Bureau of Police", "position_title": "Detective", "status": "open", "quarter": "Q1", "year": 2026},  # same opening, later
        {"department": "Planning Bureau", "position_title": "Deputy Planning Director", "status": "open", "quarter": "Q2", "year": 2025},
    ]})
    v = DashboardAggregator(store)._build_vacancies()
    dets = [x for x in v if x["position_title"] == "Detective"]
    assert len(dets) == 1                       # collapsed across quarters
    assert (dets[0]["quarter"], dets[0]["year"]) == ("Q1", 2026)   # kept the latest
    assert len(v) == 2


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
        "votes", "metrics", "vendor_spend", "commitments", "review_questions",
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
         "amount": 3000000.0, "vendor": "USDOT", "department": "Engineering / Grants",
         "adopted_date": datetime.date(2026,1,27)}]})
    r = DashboardAggregator(store)._build_resolutions()[0]
    assert r["resolution_number"] == "9-2026" and r["amount"] == 3000000.0
    assert r["department"] == "Engineering / Grants"   # now surfaced for the dossier
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
