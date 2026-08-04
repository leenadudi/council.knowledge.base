"""Applies the migration to a SCRATCH database and asserts the schema shape.
Skipped unless SCRATCH_DATABASE_URL is set. NEVER point this at production."""
import os
import pathlib
import psycopg2
import pytest

SCRATCH = os.getenv("SCRATCH_DATABASE_URL")
pytestmark = pytest.mark.skipif(not SCRATCH, reason="SCRATCH_DATABASE_URL not set")
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture()
def conn():
    c = psycopg2.connect(SCRATCH)
    yield c
    c.close()


def _apply(conn, path):
    with conn.cursor() as cur:
        cur.execute(path.read_text())
    conn.commit()


def test_migration_is_idempotent_and_creates_links(conn):
    # Base schema first. Its document_chunks references are guarded with
    # to_regclass(), so applying it before the app-owned table exists is safe.
    _apply(conn, ROOT / "sql" / "schema.sql")
    # Create document_chunks (app-owned, not SQL-created) BEFORE the migration so
    # the migration's guarded document_chunks FKs actually get added.
    from src.storage.vector_store import VectorStore
    VectorStore(_settings_for(SCRATCH)).ensure_collection()
    mig = ROOT / "sql" / "migrate_2026_07_28_schema_relationships.sql"
    _apply(conn, mig)
    _apply(conn, mig)  # second run must not error

    with conn.cursor() as cur:
        for tbl in ("departments", "department_aliases", "council_members",
                    "vendors", "meeting_attendance"):
            cur.execute("SELECT to_regclass(%s)", (tbl,))
            assert cur.fetchone()[0] is not None, tbl
        cur.execute("""SELECT COUNT(*) FROM information_schema.table_constraints
                       WHERE constraint_type='FOREIGN KEY' AND constraint_schema='public'""")
        assert cur.fetchone()[0] >= 30
        # document_chunks FKs were added once the app-created table existed
        for conname in ("fk_document_chunks_doc", "fk_document_chunks_doctype",
                        "fk_chunk_perf_chunk", "fk_expenditures_source_chunk"):
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname=%s", (conname,))
            assert cur.fetchone() is not None, conname
        # dimensions ship empty
        cur.execute("SELECT COUNT(*) FROM departments")
        assert cur.fetchone()[0] == 0


def _settings_for(url):
    # A fresh Settings instance, NOT get_settings(): mutating that shared
    # lru_cache singleton would leak the scratch URL into every other consumer
    # of get_settings() for the rest of the test process (see the matching
    # comment on tests/storage/conftest.py's _settings_for).
    from src.config import Settings
    return Settings(database_url=url)
