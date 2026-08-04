import pytest
import psycopg2.extras
from src.config import get_settings
from src.storage.sql_store import SQLStore
from src.ingestion import approval, registry

pytestmark = pytest.mark.integration

TYPE = "triage_test_widgets"
TABLE = "triage_test_widget_rows"


def _cleanup(store):
    with store.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute("DELETE FROM document_type_registry WHERE type_name = %s", (TYPE,))
        cur.execute("DELETE FROM type_proposals WHERE source_file = 'triage_approval_test.pdf'")


def test_approve_creates_table_registers_type_and_marks_approved():
    store = SQLStore(get_settings()); store.connect()
    _cleanup(store)
    payload = {"has_structured_data": True, "proposed_type_name": TYPE,
               "record_types": [{"name": TABLE, "target": "new",
                                 "description": "test widgets",
                                 "proposed_columns": [{"name": "widget_name", "type": "VARCHAR(120)"},
                                                      {"name": "qty", "type": "INTEGER"}]}]}
    with store.cursor() as cur:
        cur.execute("INSERT INTO type_proposals (source_file, proposed_type, payload) "
                    "VALUES ('triage_approval_test.pdf', %s, %s) RETURNING id",
                    (TYPE, psycopg2.extras.Json(payload)))
        pid = cur.fetchone()["id"]
    try:
        result = approval.approve_proposal(store, pid)
        assert result["created_tables"] == [TABLE]
        # table exists
        with store.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) AS t", (f"public.{TABLE}",))
            assert cur.fetchone()["t"] == TABLE
        # proposal marked approved
        assert store.get_type_proposal(pid)["status"] == "approved"
        # type is live in the registry with a compiled schema
        dt = registry.get_document_type(TYPE)
        assert dt is not None and dt.sql_targets == [TABLE]
        assert set(dt.extraction_schema.model_fields.keys()) == {TABLE}
        # generic insert works into the new table
        cid = "00000000-0000-0000-0000-000000000000"
        store.insert_dynamic_rows(TABLE, [{"widget_name": "A", "qty": 3, "bogus": "x"}],
                                  cid, "triage_approval_test.pdf")
        with store.cursor() as cur:
            cur.execute(f"SELECT widget_name, qty, source_file FROM {TABLE}")
            r = cur.fetchone()
            assert r["widget_name"] == "A" and r["qty"] == 3
    finally:
        _cleanup(store); store.close()
