"""Shared fixtures for tests/storage/ integration tests that run against a
scratch database — NEVER production.

`scratch_store` connects SQLStore to SCRATCH_DATABASE_URL, applies
sql/schema.sql (+ VectorStore.ensure_collection(), so the app-owned
document_chunks table exists — schema.sql's document_chunks FKs are guarded on
to_regclass('document_chunks') and silently no-op until that table is created)
and the entity-relationships migration once per test session, then truncates
the fact + dimension tables before every test so tests never see each other's
rows.

Any test module that uses this fixture MUST guard itself with:
    pytestmark = pytest.mark.skipif(
        not os.getenv("SCRATCH_DATABASE_URL"), reason="SCRATCH_DATABASE_URL not set")
That skip happens before fixtures are set up, so nothing here ever runs — let
alone connects to any database — when SCRATCH_DATABASE_URL is unset.
"""
from __future__ import annotations

import os
import pathlib

import psycopg2
import pytest

from src.config import Settings
from src.storage.sql_store import SQLStore
from src.storage.vector_store import VectorStore

SCRATCH = os.getenv("SCRATCH_DATABASE_URL")
ROOT = pathlib.Path(__file__).resolve().parents[2]

# Every table the backfill discovers entities from or writes FK columns into,
# plus their alias/junction tables. TRUNCATE ... CASCADE also pulls in
# dependents not listed here (e.g. evaluation_results, chunk_performance) —
# fine to clear on a disposable scratch DB. document_type_registry is
# deliberately NOT listed: it's a parent of documents/document_chunks (not a
# child), so CASCADE never touches it, and its seeded 'quarterly_report' row
# should survive across tests within a session.
_TABLES_TO_RESET = [
    "meeting_attendance", "votes", "meeting_actions", "legislation", "resolutions",
    "meetings", "appropriations", "goals", "expenditures", "metrics", "grants",
    "vacancies", "documents", "document_chunks", "evaluation_suite", "review_flags",
    "department_aliases", "council_member_aliases", "vendor_aliases",
    "departments", "council_members", "vendors",
]


def _settings_for(url: str) -> Settings:
    """A fresh Settings instance pointed at `url`. Deliberately does NOT go
    through get_settings()'s lru_cache: mutating that shared singleton would
    leak the scratch URL into every other consumer of get_settings() for the
    rest of the test process (e.g. tests/storage/test_resolution_store.py's
    own SQLStore() fixture), silently redirecting unrelated tests. A fresh
    instance still resolves every other field from env/.env exactly like
    get_settings() would — only database_url is pinned here."""
    return Settings(database_url=url)


@pytest.fixture(scope="session")
def _scratch_schema():
    """Apply schema.sql + ensure_collection() + the migration once per test
    session. Returns the scratch URL for dependents. Skips (rather than ever
    falling back to a default DB) when SCRATCH_DATABASE_URL is unset."""
    if not SCRATCH:
        pytest.skip("SCRATCH_DATABASE_URL not set")
    conn = psycopg2.connect(SCRATCH)
    try:
        with conn.cursor() as cur:
            cur.execute((ROOT / "sql" / "schema.sql").read_text())
        conn.commit()
        # document_chunks is created by the application, not by SQL. Create it
        # now so the migration's guarded document_chunks FKs actually get added.
        VectorStore(_settings_for(SCRATCH)).ensure_collection()
        with conn.cursor() as cur:
            cur.execute(
                (ROOT / "sql" / "migrate_2026_07_28_schema_relationships.sql").read_text())
        conn.commit()
    finally:
        conn.close()
    return SCRATCH


def _truncate(url: str) -> None:
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(_TABLES_TO_RESET)} RESTART IDENTITY CASCADE")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def scratch_store(_scratch_schema):
    """Truncate the fact + dimension tables, then yield a connected SQLStore
    against the scratch DB so each test starts from an empty, known state.

    Truncates on BOTH sides of the yield. The teardown truncate is not optional:
    backfill() writes real dimension rows (e.g. departments.canonical_key='fire'),
    and the pre-existing tests/storage/test_migration_relationships.py asserts
    'dimensions ship empty' (SELECT COUNT(*) FROM departments == 0) and runs AFTER
    this module alphabetically — so leaving rows behind would deterministically
    break it once SCRATCH_DATABASE_URL is set. Leave the scratch DB clean."""
    _truncate(_scratch_schema)
    store = SQLStore(_settings_for(_scratch_schema))
    store.connect()
    try:
        yield store
    finally:
        store.close()
        _truncate(_scratch_schema)
