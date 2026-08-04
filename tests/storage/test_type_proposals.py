import pytest
from src.config import get_settings
from src.storage.sql_store import SQLStore

pytestmark = pytest.mark.integration


def test_insert_and_fetch_pending_proposal():
    store = SQLStore(get_settings()); store.connect()
    store.insert_type_proposal("triage_test.pdf", "boards",
                               {"has_structured_data": True, "record_types": []})
    pending = store.get_pending_type_proposals()
    assert any(p["source_file"] == "triage_test.pdf" for p in pending)
    with store.cursor() as cur:
        cur.execute("DELETE FROM type_proposals WHERE source_file = 'triage_test.pdf'")
    store.close()
