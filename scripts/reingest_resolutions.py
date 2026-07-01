#!/usr/bin/env python3
"""Re-ingest the resolution PDFs through the Tesseract-first pipeline and validate the data.
Operator-run (needs live Supabase/Neo4j + tesseract). Usage: python3 scripts/reingest_resolutions.py"""

import glob
import logging
import os
import sys
from pathlib import Path

# Ensure src/ is on the path when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.ERROR)

from src.config import get_settings
from src.ingestion.pipeline import IngestionPipeline
from src.storage.sql_store import SQLStore


def main():
    cfg = get_settings()
    pipe = IngestionPipeline(cfg)
    pipe.initialize_stores()

    files = [f for f in sorted(glob.glob("docs/Resolutions*.pdf")) if "(1)" not in f]
    print(f"Re-ingesting {len(files)} resolutions (Tesseract-first)...")
    for f in files:
        pipe.ingest_document(f)
        print("  ingested", os.path.basename(f))

    s = SQLStore(cfg)
    s.connect()
    with s.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT parser_used FROM documents WHERE source_file ILIKE 'Resolutions%'"
        )
        print("parser_used for resolutions:", [r["parser_used"] for r in cur.fetchall()])

        cur.execute(
            "SELECT resolution_number, count(*) FROM votes"
            " GROUP BY resolution_number ORDER BY resolution_number"
        )
        print("votes per resolution (expect VARIED, not all 7):")
        for r in cur.fetchall():
            print("   ", r["resolution_number"], "->", r["count"])

        cur.execute("SELECT count(*) AS c FROM resolutions WHERE amount IS NOT NULL")
        print("resolutions with an amount:", cur.fetchone()["c"])
    s.close()


if __name__ == "__main__":
    main()
