#!/usr/bin/env python3
"""Backfill: re-extract ALL quarterly reports from EXISTING chunks (no re-parse/
re-embed) through the unified extract_quarterly path. Preserves human-set goal
user_status. Operator-run (live Supabase + ANTHROPIC_API_KEY).

Usage:
  python3 scripts/reextract_quarterly.py           # dry-run (LLM calls, no writes)
  python3 scripts/reextract_quarterly.py --write    # re-extract + write
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.extraction.sql_extractor import SQLExtractor
from src.storage.sql_store import SQLStore


def merge_user_status(existing: list[dict], fresh: list[dict]):
    """Re-apply user_status/user_status_at from existing goal rows onto freshly
    extracted ones, matching (department, year, quarter, goal_title). Returns
    (fresh_with_status, unmatched_existing)."""
    def key(r): return (r.get("department"), r.get("year"), r.get("quarter"), r.get("goal_title"))
    set_status = {key(r): r for r in existing if r.get("user_status")}
    for r in fresh:
        prior = set_status.pop(key(r), None)
        if prior:
            r["user_status"] = prior["user_status"]
            r["user_status_at"] = prior.get("user_status_at")
    return fresh, list(set_status.values())


_TABLE_INSERT = [
    ("expenditures", "insert_expenditure_rows", True),
    ("metrics", "insert_metric_rows", True),
    ("grants", "insert_grant_rows", True),
    ("vacancies", "insert_vacancy_rows", False),   # no source_file arg
    ("goals", "insert_goal_rows", True),
    ("projects", "insert_project_rows", True),
]


def main(write: bool):
    cfg = get_settings()
    store = SQLStore(cfg); store.connect()
    ext = SQLExtractor(cfg)
    with store.cursor() as cur:
        cur.execute("SELECT DISTINCT source_file, department, quarter, year "
                    "FROM document_chunks WHERE document_type='quarterly_report' "
                    "ORDER BY department, year, quarter")
        reports = [dict(r) for r in cur.fetchall()]
    print(f"Quarterly reports: {len(reports)} | mode: {'WRITE' if write else 'DRY-RUN'}\n")

    for rep in reports:
        sf, dept, q, y = rep["source_file"], rep["department"], rep["quarter"], rep["year"]
        with store.cursor() as cur:
            cur.execute("SELECT chunk_id, text FROM document_chunks WHERE source_file=%s ORDER BY chunk_id", (sf,))
            chunk_rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT department, year, quarter, goal_title, user_status, user_status_at "
                        "FROM goals WHERE source_file=%s", (sf,))
            prior_goals = [dict(r) for r in cur.fetchall()]

        class _C:  # minimal chunk shim: extract_quarterly only reads .text
            def __init__(self, t): self.text = t
        data = ext.extract_quarterly([_C(c["text"]) for c in chunk_rows],
                                     department=dept, quarter=q or "", year=y)
        counts = {k: len(v) for k, v in data.items()}
        print(f"{sf}\n   {dept} {q} {y} -> {counts}")

        if data.get("goals"):
            data["goals"], unmatched = merge_user_status(prior_goals, data["goals"])
            for u in unmatched:
                print(f"   [user_status] UNMATCHED after re-extract: {u['goal_title']!r} "
                      f"status={u['user_status']} (reconcile manually)")

        if not write:
            print()
            continue

        cid = str(chunk_rows[0]["chunk_id"])
        store.delete_structured_rows(sf)
        for key, method, has_file in _TABLE_INSERT:
            rows = data.get(key)
            if not rows:
                continue
            if has_file:
                getattr(store, method)(rows, cid, sf)
            else:
                getattr(store, method)(rows, cid)
        print(f"   wrote {sum(counts.values())} rows\n")
    store.close()


if __name__ == "__main__":
    main(write="--write" in sys.argv)
