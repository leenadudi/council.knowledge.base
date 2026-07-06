#!/usr/bin/env python3
"""Targeted re-extraction of council meeting records from already-ingested minutes.

The generic schema path was unreliable for minutes (missing dates/actions). This
uses the focused SQLExtractor.extract_meeting prompt over the chunks already in
the DB — no re-OCR. Idempotent per source_file.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.storage.sql_store import SQLStore
from src.extraction.sql_extractor import SQLExtractor


def main() -> None:
    cfg = get_settings()
    sql = SQLStore(cfg); sql.connect()
    ex = SQLExtractor(cfg)

    with sql.cursor() as c:
        c.execute("SELECT source_file FROM documents WHERE document_type='minutes' ORDER BY source_file")
        files = [r["source_file"] for r in c.fetchall()]

    print(f"minutes docs: {len(files)}")
    total_m = total_a = 0
    for sf in files:
        with sql.cursor() as c:
            c.execute("SELECT chunk_id, text FROM document_chunks WHERE source_file = %s", (sf,))
            chunks = c.fetchall()
        if not chunks:
            print(f"  - {sf[-40:]}: no chunks, skip"); continue
        res = ex.extract_meeting([x["text"] for x in chunks], source_file=sf)
        meetings, actions = res["meetings"], res["meeting_actions"]
        mdate = (meetings[0].get("meeting_date") if meetings else None)
        for a in actions:
            a.setdefault("meeting_date", mdate)
        cid = str(chunks[0]["chunk_id"])
        with sql.cursor() as c:
            c.execute("DELETE FROM meetings WHERE source_file = %s", (sf,))
            c.execute("DELETE FROM meeting_actions WHERE source_file = %s", (sf,))
        if meetings:
            sql.insert_meeting_rows(meetings, cid, sf)
        if actions:
            sql.insert_meeting_action_rows(actions, cid, sf)
        total_m += len(meetings); total_a += len(actions)
        print(f"  - {sf[-40:]}: date={mdate} meetings={len(meetings)} actions={len(actions)}")
    print(f"TOTAL: {total_m} meetings, {total_a} actions")


if __name__ == "__main__":
    main()
