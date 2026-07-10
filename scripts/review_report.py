#!/usr/bin/env python3
"""Print documents withheld from the structured tables pending review.
Operator-run (needs live DB). Usage: python3 scripts/review_report.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.storage.sql_store import SQLStore


def main():
    s = SQLStore(get_settings()); s.connect()
    try:
        flags = s.get_unresolved_review_flags()
    finally:
        s.close()

    if not flags:
        print("No documents pending review. ✅")
        return

    by_stage: dict[str, list[dict]] = {}
    for f in flags:
        by_stage.setdefault(f["stage"], []).append(f)

    print(f"{len(flags)} document(s) need review:\n")
    for stage, items in sorted(by_stage.items()):
        print(f"== {stage} ==")
        for f in items:
            detail = (f.get("detail") or "").strip().replace("\n", " ")
            print(f"  • {f['source_file']}")
            print(f"      {f['reason']}")
            if detail:
                print(f"      got: {detail[:160]}")
        print()


if __name__ == "__main__":
    main()
