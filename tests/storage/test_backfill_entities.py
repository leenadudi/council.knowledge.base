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


def test_backfill_links_whitespace_only_variant(scratch_store):
    """A department value whose ONLY occurrence carries trailing whitespace must
    still get linked. The resolver stores the alias verbatim (with the trailing
    space), so the FK-join must TRIM both sides — otherwise 'bureau of codes' never
    equals the stored alias 'bureau of codes ' and department_id stays NULL forever.
    Unlike test_backfill_discovers_and_links (which also inserts a CLEAN variant
    that masks the bug), here there is no clean variant anywhere."""
    with scratch_store.cursor() as cur:
        cur.execute("INSERT INTO expenditures (department, line_item) VALUES "
                    "('Bureau of Codes ', 'x')")          # trailing space, no clean twin
    backfill(scratch_store)
    with scratch_store.cursor() as cur:
        cur.execute("SELECT department_id FROM expenditures WHERE line_item = 'x'")
        assert cur.fetchone()["department_id"] is not None  # whitespace-only value linked
