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
        "FROM grants WHERE": [{"active": 0, "expiring": 0}],
        "GROUP BY department": [{"department": "Codes", "revised_budget": 1.0, "ytd_expended": 0.5}],
        "FROM expenditures": [{"ytd": 0, "budget": 0}],
        "MAX(year)": [{"year": None}],
        "FROM resolutions": [],
        "document_type='unclassified'": [{"c": 0}],
        "FROM documents ORDER BY": [{"department": "Codes", "quarter": "Q1", "year": 2026, "document_type": "quarterly_report"}],
    })
    out = DashboardAggregator(store).build()
    assert set(out) >= {"generated_at", "kpis", "timeline", "tables"}
    assert out["tables"]["spending_by_dept"][0]["department"] == "Codes"


class _BoomStore:
    from contextlib import contextmanager as _cm
    @_cm
    def cursor(self):
        raise RuntimeError("db down")
        yield  # pragma: no cover


def test_build_never_raises_records_errors():
    out = DashboardAggregator(_BoomStore()).build()
    assert out["kpis"] is None and out["timeline"] is None and out["tables"] is None
    assert set(out["errors"].keys()) == {"kpis", "timeline", "tables"}
