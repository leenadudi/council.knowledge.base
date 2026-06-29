# tests/storage/test_resolution_store.py
"""Integration tests for SQLStore resolution/vote insert methods.

Requires a live Postgres instance with sql/schema.sql applied.
Mark: @pytest.mark.integration  — skipped by default; run with -m integration.
"""
import uuid
import pytest

from src.storage.sql_store import SQLStore


@pytest.fixture(scope="module")
def store():
    """Open a real DB connection; skip the whole module if Postgres is unreachable."""
    s = SQLStore()
    try:
        s.connect()
    except Exception as exc:
        pytest.skip(f"Postgres unreachable: {exc}")
    yield s
    s.close()


@pytest.mark.integration
def test_insert_resolution_rows(store):
    chunk_id = str(uuid.uuid4())
    source_file = "test_resolution_store.pdf"
    rows = [
        {
            "resolution_number": "2026-R-99",
            "title": "Award contract to TestCo",
            "amount": 75000.0,
            "vendor": "TestCo LLC",
            "department": "Public Works",
            "adopted_date": "2026-03-15",
            "status": "adopted",
        }
    ]
    # Should not raise
    store.insert_resolution_rows(rows, chunk_id, source_file)

    # Verify row was inserted
    result = store.execute_query(
        f"SELECT * FROM resolutions WHERE source_file = '{source_file}'"
    )
    assert len(result) == 1
    assert result[0]["resolution_number"] == "2026-R-99"
    assert float(result[0]["amount"]) == 75000.0

    # Cleanup
    with store.cursor() as cur:
        cur.execute("DELETE FROM resolutions WHERE source_file = %s", (source_file,))


@pytest.mark.integration
def test_insert_vote_rows(store):
    chunk_id = str(uuid.uuid4())
    source_file = "test_vote_store.pdf"
    rows = [
        {
            "resolution_number": "2026-R-99",
            "council_member": "Jane Smith",
            "vote": "yes",
        },
        {
            "resolution_number": "2026-R-99",
            "council_member": "John Doe",
            "vote": "no",
        },
    ]
    store.insert_vote_rows(rows, chunk_id, source_file)

    result = store.execute_query(
        f"SELECT * FROM votes WHERE source_file = '{source_file}' ORDER BY council_member"
    )
    assert len(result) == 2
    assert result[0]["council_member"] == "Jane Smith"
    assert result[0]["vote"] == "yes"
    assert result[1]["vote"] == "no"

    # Cleanup
    with store.cursor() as cur:
        cur.execute("DELETE FROM votes WHERE source_file = %s", (source_file,))


@pytest.mark.integration
def test_insert_resolution_rows_null_optional_fields(store):
    """Optional fields (amount, adopted_date) can be None."""
    chunk_id = str(uuid.uuid4())
    source_file = "test_resolution_nulls.pdf"
    rows = [
        {
            "resolution_number": "2026-R-100",
            "title": "Minimal resolution",
            "amount": None,
            "vendor": "",
            "department": "",
            "adopted_date": None,
            "status": "",
        }
    ]
    store.insert_resolution_rows(rows, chunk_id, source_file)

    result = store.execute_query(
        f"SELECT * FROM resolutions WHERE source_file = '{source_file}'"
    )
    assert len(result) == 1
    assert result[0]["amount"] is None
    assert result[0]["adopted_date"] is None

    # Cleanup
    with store.cursor() as cur:
        cur.execute("DELETE FROM resolutions WHERE source_file = %s", (source_file,))
