#!/usr/bin/env python3
"""Collapse duplicate grant rows WITHIN each source file.

The unified quarterly extraction used to emit the same grant several times per
document under name variants ("CDBG (…)" vs "… (CDBG) Program"); the pipeline
now collapses those at merge time (entity_keys.dedupe_grant_rows), and this
script applies the identical rule retroactively so old rows match what the
fixed pipeline would produce. Rows for the same grant in DIFFERENT files are
per-quarter snapshots and are intentionally kept — the dashboard's read-time
dedupe handles those.

Usage:
  python scripts/dedupe_grants_table.py           # dry run — print what would go
  python scripts/dedupe_grants_table.py --apply   # actually delete
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ingestion.entity_keys import dedupe_grant_rows
from src.storage.sql_store import SQLStore


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    store = SQLStore()
    with store.cursor() as cur:
        cur.execute("SELECT * FROM grants ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]

    by_file: dict = defaultdict(list)
    for r in rows:
        by_file[r["source_file"]].append(r)

    doomed: list[dict] = []
    for source_file, file_rows in by_file.items():
        keep_ids = {id(r) for r in dedupe_grant_rows(file_rows)}
        doomed.extend(r for r in file_rows if id(r) not in keep_ids)

    print(f"{len(rows)} grant rows; {len(doomed)} within-file duplicates to remove:")
    for r in sorted(doomed, key=lambda r: (r["source_file"], r["id"])):
        print(f"  id={r['id']:>4}  {r['grant_name'][:60]!r:62}  {r['source_file'][:60]}")

    if not doomed:
        return
    if not apply:
        print("\nDry run — re-run with --apply to delete.")
        return
    with store.cursor() as cur:
        cur.execute("DELETE FROM grants WHERE id = ANY(%s)", ([r["id"] for r in doomed],))
    print(f"Deleted {len(doomed)} rows.")


if __name__ == "__main__":
    main()
