import os
import pytest
from src.storage.sql_store import SQLStore
from scripts.backfill_entities_2026_07_28 import backfill

pytestmark = pytest.mark.skipif(
    not os.getenv("SCRATCH_DATABASE_URL"), reason="SCRATCH_DATABASE_URL not set")


def test_backfill_discovers_and_links(scratch_store):
    with scratch_store.cursor() as cur:
        cur.execute("INSERT INTO expenditures (department, line_item) VALUES "
                    "('Bureau of Fire', 'a'), ('Bureau of Fire ', 'b')")
        cur.execute("INSERT INTO resolutions (resolution_number, vendor) VALUES "
                    "('21-2026', 'Lamar Advertising, LLC')")
    counts = backfill(scratch_store)
    with scratch_store.cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM departments WHERE canonical_key='fire'")
        assert cur.fetchone()["c"] == 1                     # both variants -> one dept
        cur.execute("SELECT COUNT(*) c FROM expenditures WHERE department_id IS NOT NULL")
        assert cur.fetchone()["c"] == 2                     # both rows linked
        cur.execute("SELECT COUNT(*) c FROM vendors")
        assert cur.fetchone()["c"] == 1
    assert counts["departments"] >= 1
