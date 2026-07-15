import json
from src.ingestion.schemas.triage import TriageResult
from src.ingestion.triage import schema_summary


class _FakeCur:
    def __init__(self, rows): self._rows = rows
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeStore:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCur(self._rows)


def test_schema_summary_groups_columns_by_table():
    rows = [
        {"table_name": "grants", "column_name": "department", "data_type": "character varying"},
        {"table_name": "grants", "column_name": "amount", "data_type": "numeric"},
        {"table_name": "vacancies", "column_name": "position_title", "data_type": "character varying"},
    ]
    out = schema_summary(_FakeStore(rows))
    assert "grants(department, amount)" in out
    assert "vacancies(position_title)" in out


from src.ingestion.triage import run_triage, build_triage_prompt


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _SeqClient:
    def __init__(self, payloads): self._p = list(payloads); self._i = 0
    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k):
            o = self._o; p = o._p[min(o._i, len(o._p) - 1)]; o._i += 1
            return _FakeMsg(p)
    @property
    def messages(self): return _SeqClient._M(self)


def test_run_triage_returns_validated_result():
    good = json.dumps({"has_structured_data": True, "proposed_type_name": "boards",
        "record_types": [{"name": "board_member", "target": "new", "match_confidence": 0.9,
            "proposed_columns": [{"name": "board", "type": "VARCHAR(120)"}],
            "sample_rows": [{"board": "Audit Committee"}]}]})
    r = run_triage("some text", "grants(department, amount)", _SeqClient([good]))
    assert r.has_structured_data and r.record_types[0].name == "board_member"


def test_run_triage_safe_on_bad_json():
    r = run_triage("x", "grants(department)", _SeqClient(["not json", "still not"]))
    assert r.has_structured_data is False and r.record_types == []


def test_build_triage_prompt_includes_schema_and_rules():
    p = build_triage_prompt("BODY TEXT", "grants(department, amount)")
    assert "grants(department, amount)" in p
    assert "existing" in p and "BODY TEXT" in p
    # required refinements: confidence must be demanded, prose must be excluded
    assert "match_confidence" in p and "REQUIRED" in p
    assert "narrative prose" in p


def test_triage_result_parses_fit_and_new():
    payload = json.dumps({
        "has_structured_data": True,
        "proposed_type_name": "boards_commissions",
        "record_types": [
            {"name": "board_member", "target": "new", "match_confidence": 0.9,
             "proposed_columns": [{"name": "board", "type": "VARCHAR(120)"},
                                  {"name": "member_name", "type": "VARCHAR(120)"}],
             "sample_rows": [{"board": "Audit Committee", "member_name": "Ed Jaroch"}]},
            {"name": "appointment_ref", "target": "existing", "existing_table": "resolutions",
             "column_mapping": {"resolution": "resolution_number"}, "match_confidence": 0.7,
             "sample_rows": [{"resolution": "31-2023"}]},
        ],
    })
    r = TriageResult.model_validate_json(payload)
    assert r.has_structured_data is True
    assert r.record_types[0].target == "new"
    assert r.record_types[0].proposed_columns[0].name == "board"
    assert r.record_types[1].existing_table == "resolutions"
    assert r.record_types[1].column_mapping == {"resolution": "resolution_number"}
