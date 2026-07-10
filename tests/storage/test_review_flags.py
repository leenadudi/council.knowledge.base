import pytest

from src.config import get_settings
from src.storage.sql_store import SQLStore


@pytest.mark.integration
def test_insert_and_read_review_flag():
    s = SQLStore(get_settings()); s.connect()
    try:
        s.insert_review_flag("z-test.pdf", "validate", "bad number", "2026-2026")
        flags = s.get_unresolved_review_flags()
        assert any(f["source_file"] == "z-test.pdf" and f["reason"] == "bad number"
                   for f in flags)
    finally:
        with s.cursor() as cur:
            cur.execute("DELETE FROM review_flags WHERE source_file = 'z-test.pdf'")
        s.close()
