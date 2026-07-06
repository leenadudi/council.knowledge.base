#!/usr/bin/env python3
"""One-off: extract department goals from already-ingested 2026 quarterly reports.

No re-OCR / re-ingest — reads the goal-containing chunks already in the DB, runs
LLM extraction per report, and writes the `goals` table. Idempotent per source_file.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.storage.sql_store import SQLStore
from src.extraction.sql_extractor import SQLExtractor

DDL = """
CREATE TABLE IF NOT EXISTS goals (
    id SERIAL PRIMARY KEY, department VARCHAR(150), year INTEGER, quarter VARCHAR(5),
    goal_title TEXT, description TEXT, target TEXT, status VARCHAR(80),
    source_chunk_id UUID, source_file VARCHAR(255), ingested_at TIMESTAMP DEFAULT NOW()
)
"""


def main() -> None:
    cfg = get_settings()
    sql = SQLStore(cfg); sql.connect()
    ex = SQLExtractor(cfg)

    with sql.cursor() as c:
        c.execute(DDL)
        c.execute("CREATE INDEX IF NOT EXISTS idx_goals_dept ON goals(department, year)")
        c.execute(
            "SELECT source_file, department, quarter, year FROM documents "
            "WHERE document_type='quarterly_report' AND (year = 2026 OR source_file ILIKE '%2026%') "
            "ORDER BY department"
        )
        reports = c.fetchall()

    print(f"2026 quarterly reports: {len(reports)}")
    total = 0
    for r in reports:
        sf, dept, q, yr = r["source_file"], r["department"], r["quarter"], r["year"]
        with sql.cursor() as c:
            c.execute(
                "SELECT chunk_id, text FROM document_chunks "
                "WHERE source_file = %s AND text ILIKE '%%goal%%'", (sf,)
            )
            chunks = c.fetchall()
        if not chunks:
            print(f"  - {dept or sf[:30]}: no goal section, skip")
            continue
        rows = ex.extract_goals([x["text"] for x in chunks],
                                department=dept or "", quarter=q or "", year=yr)
        with sql.cursor() as c:
            c.execute("DELETE FROM goals WHERE source_file = %s", (sf,))
        if rows:
            sql.insert_goal_rows(rows, str(chunks[0]["chunk_id"]), sf)
        total += len(rows)
        print(f"  - {dept or sf[:30]}: {len(rows)} goals")
    print(f"TOTAL goals extracted: {total}")


if __name__ == "__main__":
    main()
