#!/usr/bin/env python3
"""Re-extract grants strictly (external awards only) from already-ingested quarterly
reports. Reuses chunks already in the DB — no re-OCR. Idempotent per source_file.

Replaces the noisy generic-extractor grants (which over-counted budget/spending
figures) with SQLExtractor.extract_grants output.
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
        c.execute("SELECT source_file, department FROM documents "
                  "WHERE document_type='quarterly_report' ORDER BY department")
        reports = c.fetchall()

    print(f"quarterly reports: {len(reports)}")
    total = 0
    for r in reports:
        sf, dept = r["source_file"], r["department"]
        with sql.cursor() as c:
            c.execute("SELECT chunk_id, text FROM document_chunks "
                      "WHERE source_file = %s AND text ILIKE '%%grant%%'", (sf,))
            chunks = c.fetchall()
        # always clear old (noisy) grants for this file first, even if 0 now
        with sql.cursor() as c:
            c.execute("DELETE FROM grants WHERE source_file = %s", (sf,))
        if not chunks:
            print(f"  - {dept or sf[:30]}: no grant mentions, cleared")
            continue
        rows = ex.extract_grants([x["text"] for x in chunks], department=dept or "")
        if rows:
            sql.insert_grant_rows(rows, str(chunks[0]["chunk_id"]), sf)
        total += len(rows)
        print(f"  - {dept or sf[:30]}: {len(rows)} grants")
    print(f"TOTAL grants after re-extraction: {total}")


if __name__ == "__main__":
    main()
