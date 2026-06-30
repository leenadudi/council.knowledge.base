# tests/dashboard/test_aggregator.py
import datetime
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
        "FROM grants WHERE": [{"active": 12, "expiring": 3}],
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
    assert kpis["latest_period"] == {"year": 2026, "quarter": "Q1"}
    assert kpis["report_coverage"] == {"filed": 8, "total_departments": 14}
    assert kpis["resolutions_count"] == 0
    assert kpis["unclassified_docs"] == 1
