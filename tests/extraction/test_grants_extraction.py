"""merge_quarterly_parts must not emit the same grant twice for one document —
the model routinely re-extracts a grant under name variants when it appears in
several sections of a report (overview + program detail), which exact-row dedup
cannot catch."""
from src.extraction.sql_extractor import SQLExtractor


def _grant(name, amount=None, status=""):
    return {"grant_name": name, "amount": amount, "status": status,
            "source_text": "q", "confidence": "high"}


def test_merge_quarterly_collapses_grant_name_variants():
    parts = [
        {"grants": [_grant("Community Development Block Grant (CDBG)", 350000.0, "active"),
                    _grant("CDBG (Community Development Block Grant)")]},
        {"grants": [_grant("Community Development Block Grant (CDBG) Program"),
                    _grant("TASA Grant", 1000000.0, "awarded")]},
    ]
    out = SQLExtractor.merge_quarterly_parts(parts, "DBHD", "Q1", 2026)
    rows = out["grants"]
    assert len(rows) == 2
    # the most complete CDBG variant (the one with an amount) is the survivor
    cdbg = [r for r in rows if "CDBG" in r["grant_name"]]
    assert len(cdbg) == 1 and cdbg[0]["amount"] == 350000.0
    assert any(r["grant_name"] == "TASA Grant" for r in rows)
    assert all(r["department"] == "DBHD" for r in rows)


def test_merge_quarterly_keeps_distinct_grants_and_subawards():
    parts = [
        {"grants": [_grant("Community Development Block Grant (CDBG)"),
                    _grant("Community Development Block Grant (CDBG) - Broad Street Market", 1000000.0),
                    _grant("CDBG-CV (Community Development Block Grant CARES Act)", 5600000.0)]},
    ]
    out = SQLExtractor.merge_quarterly_parts(parts, "DBHD", "Q1", 2026)
    assert len(out["grants"]) == 3
