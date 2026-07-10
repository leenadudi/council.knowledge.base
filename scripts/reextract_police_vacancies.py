#!/usr/bin/env python3
"""Verification harness for the vacancy-extraction fix (2026-07-10).

Re-extracts vacancies for the 4 Bureau of Police QUARTERLY reports from their
ALREADY-INGESTED chunks (no re-parse / re-embed — minimal LLM spend: one focused
extraction call per report). Proves the new keyword-selected extract_vacancies path
captures the vacancy sections the old routes_to_sql() gate dropped, and that the
open_count column is populated.

Operator-run (needs live Supabase + ANTHROPIC_API_KEY). Usage:
  python3 scripts/reextract_police_vacancies.py            # dry-run: show what WOULD change
  python3 scripts/reextract_police_vacancies.py --write    # actually re-extract + write
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.extraction.sql_extractor import SQLExtractor
from src.storage.sql_store import SQLStore

POLICE_QUARTERLY = "document_type='quarterly_report' AND department ILIKE '%police%'"


def _fmt(rows):
    return "\n".join(
        f"      {r['quarter']} {r['year']:>4}  {r['position_title']:<24} "
        f"{r['status']:<7} count={r.get('open_count')}"
        for r in rows
    ) or "      (none)"


def main(write: bool):
    cfg = get_settings()
    store = SQLStore(cfg)
    store.connect()
    ext = SQLExtractor(cfg)

    with store.cursor() as cur:
        cur.execute(f"SELECT DISTINCT source_file, department, quarter, year "
                    f"FROM document_chunks WHERE {POLICE_QUARTERLY} ORDER BY year, quarter")
        reports = [dict(r) for r in cur.fetchall()]

    print(f"Police quarterly reports found: {len(reports)}")
    print(f"Mode: {'WRITE' if write else 'DRY-RUN (no DB changes)'}\n")

    for rep in reports:
        sf, dept, q, y = rep["source_file"], rep["department"], rep["quarter"], rep["year"]
        with store.cursor() as cur:
            # vacancy-bearing chunk texts + a representative chunk_id for source ref
            cur.execute("SELECT chunk_id, text FROM document_chunks WHERE source_file=%s "
                        "ORDER BY chunk_id", (sf,))
            chunks = [dict(r) for r in cur.fetchall()]
            # existing (pre-fix) vacancy rows for this report
            cur.execute("SELECT position_title, status, quarter, year, open_count FROM vacancies "
                        "WHERE source_chunk_id IN (SELECT chunk_id FROM document_chunks WHERE source_file=%s) "
                        "ORDER BY position_title", (sf,))
            before = [dict(r) for r in cur.fetchall()]

        vacancy_texts = [c["text"] for c in chunks if "vacan" in c["text"].lower()]
        rep_chunk_id = str(chunks[0]["chunk_id"]) if chunks else None

        print(f"=== {sf}")
        print(f"    {dept} {q} {y}  | {len(chunks)} chunks, {len(vacancy_texts)} vacancy-bearing")
        print(f"    BEFORE (old routes_to_sql path):\n{_fmt(before)}")

        if not vacancy_texts or rep_chunk_id is None:
            print("    -> no vacancy text; skipping\n")
            continue

        new_rows = ext.extract_vacancies(vacancy_texts, department=dept, quarter=q, year=y)
        # normalize open_count for display parity
        for r in new_rows:
            r.setdefault("open_count", r.get("count"))
        print(f"    AFTER  (new keyword path):\n{_fmt(new_rows)}")

        if write:
            with store.cursor() as cur:
                cur.execute("DELETE FROM vacancies WHERE source_chunk_id IN "
                            "(SELECT chunk_id FROM document_chunks WHERE source_file=%s)", (sf,))
            store.insert_vacancy_rows(new_rows, rep_chunk_id)
            print(f"    -> wrote {len(new_rows)} rows (replaced {len(before)})")
        print()

    store.close()


if __name__ == "__main__":
    main(write="--write" in sys.argv)
