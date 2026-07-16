from src.query.classifier import build_classify_prompt, _CLASSIFY_PROMPT
from src.ingestion import registry
from src.ingestion.schema_compiler import compile_type_schema
from src.models import DocumentType


def test_prompt_lists_approved_data_driven_tables():
    schema = compile_type_schema("m4_test_type", [
        {"name": "m4_widgets", "proposed_columns": [
            {"name": "widget_name", "type": "VARCHAR(50)"},
            {"name": "qty", "type": "INTEGER"}]}])
    registry.register(DocumentType(name="m4_test_type", description="test",
                                   sql_targets=["m4_widgets"], extraction_schema=schema))
    try:
        p = build_classify_prompt("how many widgets are there")
        # the approved table + its columns are visible to the router
        assert "m4_widgets(id, widget_name, qty, source_chunk_id, source_file)" in p
        # built-in schema + guards are preserved, and the question is injected
        assert "quarter = 'Q1'" in p
        assert "resolutions(" in p
        assert "how many widgets are there" in p
    finally:
        registry.unregister("m4_test_type")   # don't leak into other tests


def test_prompt_builds_with_no_data_driven_types():
    # Formatting must succeed even when there are no data-driven tables (placeholder empty).
    assert "{question}" not in build_classify_prompt("x")
    assert "{data_driven_tables}" in _CLASSIFY_PROMPT  # raw template still has the slot
