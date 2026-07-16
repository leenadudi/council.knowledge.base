import json
from src.ingestion.schema_compiler import compile_type_schema


def _boards_record_types():
    return [
        {"name": "board_members",
         "proposed_columns": [
             {"name": "board_name", "type": "VARCHAR(255)"},
             {"name": "member_name", "type": "VARCHAR(255)"},
             {"name": "total_seats", "type": "INTEGER"},
             {"name": "is_vacant", "type": "BOOLEAN"},
             {"name": "term_expiration_date", "type": "DATE"},
             {"name": "id", "type": "INTEGER"},          # standard col — must be dropped
             {"name": "source_file", "type": "VARCHAR(255)"},  # standard — dropped
         ]},
        {"name": "board_structure",
         "proposed_columns": [{"name": "board_name", "type": "VARCHAR(255)"},
                              {"name": "quorum", "type": "INTEGER"}]},
    ]


def test_compile_builds_model_with_target_fields_and_row_types():
    Model = compile_type_schema("boards", _boards_record_types())
    # top-level fields are the record-type (table) names
    assert set(Model.model_fields.keys()) == {"board_members", "board_structure"}
    # validates a payload; standard columns are NOT part of the extraction contract
    payload = json.dumps({"board_members": [
        {"board_name": "Audit", "member_name": None, "total_seats": 5, "is_vacant": True,
         "term_expiration_date": None, "source_text": "x", "confidence": "high"}]})
    data = Model.model_validate_json(payload).model_dump()
    row = data["board_members"][0]
    assert row["board_name"] == "Audit" and row["is_vacant"] is True and row["total_seats"] == 5
    assert "id" not in row and "source_file" not in row       # standard cols excluded
    assert row["source_text"] == "x" and row["confidence"] == "high"


def test_compile_defaults_missing_optional_columns_to_none():
    Model = compile_type_schema("boards", _boards_record_types())
    payload = json.dumps({"board_members": [
        {"board_name": "Plumbing", "source_text": "y", "confidence": "medium"}]})
    row = Model.model_validate_json(payload).model_dump()["board_members"][0]
    assert row["member_name"] is None and row["is_vacant"] is None
