import pytest
from src.config import get_settings
from src.storage.sql_store import SQLStore


@pytest.mark.integration
def test_insert_and_delete_project_rows():
    s = SQLStore(get_settings()); s.connect()
    cid = "00000000-0000-0000-0000-000000000001"
    try:
        s.insert_project_rows(
            [{"department": "ZTest Dept", "project_name": "Porch Lights",
              "description": "camera + lighting pilot", "status": "ongoing",
              "funding_source": "LLES-2023", "quarter": "Q1", "year": 2025}],
            cid, "z-projtest.pdf")
        rows = s.execute_query("SELECT project_name, funding_source FROM projects WHERE source_file='z-projtest.pdf'")
        assert rows and rows[0]["project_name"] == "Porch Lights" and rows[0]["funding_source"] == "LLES-2023"
        s.delete_structured_rows("z-projtest.pdf")
        rows2 = s.execute_query("SELECT 1 FROM projects WHERE source_file='z-projtest.pdf'")
        assert rows2 == []
    finally:
        with s.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE source_file = 'z-projtest.pdf'")
        s.close()
