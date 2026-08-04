import re
import pytest
from src.storage.ddl import build_create_table, DDLError


def _cols():
    return [
        {"name": "board_name", "type": "VARCHAR(255)"},
        {"name": "total_seats", "type": "INTEGER"},
        {"name": "is_vacant", "type": "BOOLEAN"},
        {"name": "term_expiration_date", "type": "DATE"},
        {"name": "notes", "type": "TEXT"},
        {"name": "amount", "type": "DECIMAL(15,2)"},
    ]


def test_build_create_table_valid():
    sql = build_create_table("board_members", _cols())
    assert sql.startswith("CREATE TABLE IF NOT EXISTS board_members")
    # domain columns present
    assert "board_name VARCHAR(255)" in sql and "is_vacant BOOLEAN" in sql
    # standard columns always added
    for std in ("id SERIAL PRIMARY KEY", "source_chunk_id UUID",
                "source_file VARCHAR(255)", "ingested_at TIMESTAMP"):
        assert std in sql
    # source_file index emitted
    assert "CREATE INDEX IF NOT EXISTS" in sql and "source_file" in sql
    # never destructive
    assert "DROP" not in sql.upper() and "ALTER" not in sql.upper()


def test_standard_columns_proposed_by_agent_are_deduped():
    cols = _cols() + [{"name": "id", "type": "INTEGER"},
                      {"name": "source_file", "type": "VARCHAR(255)"}]
    sql = build_create_table("board_members", cols)
    # the standard definitions win; the agent's loose duplicates are dropped
    assert sql.count("id SERIAL PRIMARY KEY") == 1
    assert "id INTEGER" not in sql
    assert sql.count("source_file VARCHAR(255)") == 1


@pytest.mark.parametrize("bad_table", [
    "board members", "board;DROP TABLE x", "1board", "board-members", "SELECT", ""])
def test_rejects_bad_table_names(bad_table):
    with pytest.raises(DDLError):
        build_create_table(bad_table, _cols())


@pytest.mark.parametrize("bad_col", [
    {"name": "board name", "type": "VARCHAR(255)"},
    {"name": "board_name); DROP TABLE x; --", "type": "TEXT"},
    {"name": "ok", "type": "VARCHAR(255); DROP TABLE x"},
    {"name": "ok", "type": "BIGSERIAL"},
    {"name": "ok", "type": "JSONB"},
])
def test_rejects_bad_columns(bad_col):
    with pytest.raises(DDLError):
        build_create_table("board_members", [bad_col])


def test_rejects_no_domain_columns():
    with pytest.raises(DDLError):
        build_create_table("board_members", [])
