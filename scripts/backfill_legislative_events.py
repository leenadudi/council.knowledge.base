"""Rebuild legislative_items / legislative_events from raw rows.

Fully derived data: safe to re-run any time (full rebuild inside one
transaction). Never call with production DATABASE_URL until the migration has
been applied by a human (repo rule). No LLM calls.

Usage:
    python scripts/backfill_legislative_events.py [--dry-run]
"""
import argparse
import sys
from collections import Counter

sys.path.insert(0, ".")

from src.ingestion.legislative_events import (
    LegislativeEvent, doc_status_events, item_key, item_kind, parse_events,
)
from src.storage.sql_store import SQLStore


def _fetch(store, sql):
    with store.cursor() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def rebuild(store: SQLStore, dry_run: bool = False) -> dict:
    actions = _fetch(store, "SELECT * FROM meeting_actions ORDER BY meeting_date, id")
    resolutions = _fetch(store, "SELECT * FROM resolutions ORDER BY id")
    legislation = _fetch(store, "SELECT * FROM legislation ORDER BY id")
    votes = _fetch(store, "SELECT * FROM votes ORDER BY id")

    stats = Counter()
    if dry_run:
        for a in actions:
            kind = item_kind(a.get("item_type"), a.get("item_number"))
            if not kind:
                continue
            if not item_key(a.get("item_number")):
                stats["skipped_no_number"] += 1
                continue
            evs = parse_events(a.get("item_type"), a.get("action"))
            if evs:
                stats["events"] += len(evs)
            elif (a.get("action") or "").strip():
                stats["flagged"] += 1
            stats["items_seen"] += 1
        return dict(stats)

    with store.transaction():
        with store.cursor() as cur:
            cur.execute("DELETE FROM legislative_events")

        for a in actions:
            kind = item_kind(a.get("item_type"), a.get("item_number"))
            if not kind:
                continue
            if not item_key(a.get("item_number")):
                stats["skipped_no_number"] += 1
                continue
            item_id = store.resolve_legislative_item(
                kind, a["item_number"], a.get("title") or "", a.get("meeting_date"))
            with store.cursor() as cur:
                cur.execute("UPDATE meeting_actions SET legislative_item_id=%s WHERE id=%s",
                            (item_id, a["id"]))
            evs = parse_events(a.get("item_type"), a.get("action"))
            if evs:
                store._insert_legislative_events(
                    item_id, evs, event_date=a.get("meeting_date"), origin="minutes",
                    meeting_action_id=a["id"], source_chunk_id=a.get("source_chunk_id"),
                    source_file=a.get("source_file"))
                stats["events"] += len(evs)
            elif (a.get("action") or "").strip():
                store.insert_review_flag(
                    a.get("source_file") or "", "normalize", "unmapped meeting action",
                    f"{a.get('item_type')} {a.get('item_number')}: {a.get('action')}")
                stats["flagged"] += 1

        for kind, rows, number_col in (("resolution", resolutions, "resolution_number"),
                                       ("ordinance", legislation, "bill_number")):
            for r in rows:
                if not item_key(r.get(number_col)):
                    stats["skipped_no_number"] += 1
                    continue
                item_id = store.resolve_legislative_item(
                    kind, r[number_col], r.get("title") or "", r.get("adopted_date"))
                table = "resolutions" if kind == "resolution" else "legislation"
                with store.cursor() as cur:
                    cur.execute(f"UPDATE {table} SET legislative_item_id=%s WHERE id=%s",
                                (item_id, r["id"]))
                evs = [LegislativeEvent(e) for e in doc_status_events(r.get("status"))]
                if evs:
                    store._insert_legislative_events(
                        item_id, evs, event_date=r.get("adopted_date"), origin="document",
                        meeting_action_id=None, source_chunk_id=r.get("source_chunk_id"),
                        source_file=r.get("source_file"))
                    stats["events"] += len(evs)

        for v in votes:
            if not item_key(v.get("resolution_number")):
                continue
            item_id = store.resolve_legislative_item("resolution", v["resolution_number"])
            with store.cursor() as cur:
                cur.execute("UPDATE votes SET legislative_item_id=%s WHERE id=%s",
                            (item_id, v["id"]))

        with store.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM legislative_items")
            stats["items"] = cur.fetchone()["n"]
    return dict(stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report counts; write nothing")
    args = ap.parse_args()
    store = SQLStore()
    store.connect()
    stats = rebuild(store, dry_run=args.dry_run)
    mode = "DRY RUN — nothing written" if args.dry_run else "rebuilt"
    print(f"Legislative status backfill ({mode}):")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    store.close()


if __name__ == "__main__":
    main()
