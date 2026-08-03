"""One-shot: populate entity dimensions + FK columns from data already in the
tables. Deterministic, no LLM. Run AFTER sql/migrate_2026_07_28_schema_
relationships.sql. Idempotent — safe to re-run; converges as aliases are added.

Usage:  DATABASE_URL=<scratch-or-prod> python -m scripts.backfill_entities_2026_07_28
"""
from __future__ import annotations

from src.storage.sql_store import SQLStore

_DEPT_TABLES = [
    "expenditures", "metrics", "grants", "vacancies", "documents", "resolutions",
    "appropriations", "goals", "document_chunks", "evaluation_suite",
]


def _distinct(cur, table, col):
    cur.execute(
        f"SELECT DISTINCT {col} AS v FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
    return [r["v"] for r in cur.fetchall()]


def backfill(store: SQLStore) -> dict:
    """Discover entity dimensions from existing distinct strings, link FK columns
    by alias join, and parse attendance for existing meetings.

    Wrapped in a single store.transaction() so the whole backfill is atomic AND
    self-safe: resolve_department_id / resolve_member_id / resolve_vendor_id /
    _resolve_attendance each open their own store.cursor(). Outside a
    transaction, that path's _get_live_conn() issues a defensive "reconnect if
    idle" rollback that would discard this function's own pending work — the
    same row-loss hazard fixed for the ingest insert_*_rows methods (see
    sql_store.py). Inside a transaction, store.cursor() reuses the transaction
    connection and defers its commit to transaction()'s exit, so those nested
    cursor opens (including the ones inside the outer `with store.cursor()`
    block below) are safe.
    """
    counts = {"departments": 0, "members": 0, "vendors": 0, "attendance": 0}
    with store.transaction():
        with store.cursor() as cur:
            # 1. discover dimensions (auto-create) from every raw string in use
            for table in _DEPT_TABLES:
                for name in _distinct(cur, table, "department"):
                    if store.resolve_department_id(name):
                        counts["departments"] += 1
            for name in _distinct(cur, "votes", "council_member") \
                    + _distinct(cur, "meetings", "president"):
                if store.resolve_member_id(name):
                    counts["members"] += 1
            for name in _distinct(cur, "resolutions", "vendor"):
                if store.resolve_vendor_id(name):
                    counts["vendors"] += 1
            # sponsors are match-only (committees must not become members)
            for name in _distinct(cur, "legislation", "sponsor"):
                store.resolve_member_id(name, create=False)

            # 2. set FK columns by alias join (idempotent)
            for table in _DEPT_TABLES:
                cur.execute(
                    f"UPDATE {table} t SET department_id = da.department_id "
                    f"FROM department_aliases da "
                    f"WHERE LOWER(TRIM(t.department)) = LOWER(da.alias) AND t.department_id IS NULL")
            cur.execute(
                "UPDATE votes v SET member_id = a.member_id FROM council_member_aliases a "
                "WHERE LOWER(TRIM(v.council_member)) = LOWER(a.alias) AND v.member_id IS NULL")
            cur.execute(
                "UPDATE meetings m SET president_id = a.member_id FROM council_member_aliases a "
                "WHERE LOWER(TRIM(m.president)) = LOWER(a.alias) AND m.president_id IS NULL")
            cur.execute(
                "UPDATE legislation l SET sponsor_id = a.member_id FROM council_member_aliases a "
                "WHERE LOWER(TRIM(l.sponsor)) = LOWER(a.alias) AND l.sponsor_id IS NULL")
            cur.execute(
                "UPDATE resolutions r SET vendor_id = va.vendor_id FROM vendor_aliases va "
                "WHERE LOWER(TRIM(r.vendor)) = LOWER(va.alias) AND r.vendor_id IS NULL")

            # 3. attendance for existing meetings (raw TEXT stays as audit trail)
            cur.execute("SELECT id, members_present, members_present_names, "
                        "members_absent_names FROM meetings")
            for m in cur.fetchall():
                pairs = store._resolve_attendance(m["members_present_names"], m["members_absent_names"])
                store._write_attendance(cur, m["id"], pairs, m["members_present"])
                counts["attendance"] += 1
    return counts


if __name__ == "__main__":
    store = SQLStore()
    store.connect()
    try:
        print(backfill(store))
    finally:
        store.close()
