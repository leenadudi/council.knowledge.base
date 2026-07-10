"""Unit tests for the keyword-based vacancy extraction path.

Vacancies in quarterly reports live in a 'Department Vacancy Updates' block that
the content-type classifier frequently tags as org_data/narrative/header — content
types that do NOT route to SQL. The old path only extracted vacancies from chunks
the classifier happened to tag table/metrics, silently dropping ~68% of them.

extract_vacancies mirrors extract_grants/extract_goals: it takes the vacancy-bearing
chunk texts directly (selected by keyword upstream) and runs a focused extraction,
bypassing the fragile routes_to_sql() gate. It also captures the position COUNT that
the reports state (e.g. "Patrol Officer- (25)").
"""
import json

from src.extraction.sql_extractor import SQLExtractor


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _FakeClient:
    def __init__(self, payload): self._p = payload

    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k): return _FakeMsg(self._o._p)

    @property
    def messages(self): return _FakeClient._M(self)


def test_extract_vacancies_returns_rows_tagged_with_period():
    payload = json.dumps({"vacancies": [
        {"position_title": "Patrol Officer", "status": "open", "count": 25,
         "source_text": "Patrol Officer- (25)", "confidence": "high"},
        {"position_title": "Detective", "status": "open", "count": 4,
         "source_text": "Detective- (4)", "confidence": "high"},
    ]})
    ext = SQLExtractor(llm=_FakeClient(payload))
    rows = ext.extract_vacancies(
        ["Department Vacancy Updates: Vacancies: Patrol Officer- (25) Detective- (4)"],
        department="Bureau of Police", quarter="Q1", year=2025,
    )
    assert len(rows) == 2
    patrol = next(r for r in rows if r["position_title"] == "Patrol Officer")
    assert patrol["status"] == "open"
    assert patrol["count"] == 25
    assert patrol["department"] == "Bureau of Police"
    assert patrol["quarter"] == "Q1"
    assert patrol["year"] == 2025


def test_extract_vacancies_drops_low_confidence():
    payload = json.dumps({"vacancies": [
        {"position_title": "Supervisor", "status": "open", "count": 3,
         "source_text": "Supervisors- (3)", "confidence": "high"},
        {"position_title": "Ghost Role", "status": "open", "count": 1,
         "source_text": "unclear", "confidence": "low"},
    ]})
    ext = SQLExtractor(llm=_FakeClient(payload))
    rows = ext.extract_vacancies(["..."], department="X", quarter="Q2", year=2025)
    assert [r["position_title"] for r in rows] == ["Supervisor"]


def test_extract_vacancies_empty_input_returns_empty():
    ext = SQLExtractor(llm=_FakeClient("{}"))
    assert ext.extract_vacancies([], department="X", quarter="Q1", year=2025) == []


def test_extract_vacancies_never_raises_on_bad_json():
    ext = SQLExtractor(llm=_FakeClient("not json at all"))
    assert ext.extract_vacancies(["text"], department="X", quarter="Q1", year=2025) == []
