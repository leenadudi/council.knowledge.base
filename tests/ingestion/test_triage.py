import json
from src.ingestion.schemas.triage import TriageResult


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
