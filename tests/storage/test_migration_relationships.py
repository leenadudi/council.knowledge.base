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
    # Minimal base schema first, then the migration twice (idempotency check).
    _apply(conn, ROOT / "sql" / "schema.sql")
    from src.storage.vector_store import VectorStore  # creates document_chunks
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
        # dimensions ship empty
        cur.execute("SELECT COUNT(*) FROM departments")
        assert cur.fetchone()[0] == 0


def _settings_for(url):
    from src.config import get_settings
    s = get_settings()
    s.database_url = url
    return s
