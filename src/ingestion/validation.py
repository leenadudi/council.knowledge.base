"""Post-extraction sanity checks. Returns a list of problems; empty == valid.

Validators are keyed by document type so a bad extraction (e.g. a resolution number
read as the year off a garbled scan) is caught before it reaches the structured tables.
"""
from __future__ import annotations

import re

_RES_NUM_RE = re.compile(r"^\d{1,4}-(\d{4})$")
_MAX_VOTES = 15


def _validate_resolution(extracted: dict) -> list[str]:
    problems: list[str] = []
    rows = extracted.get("resolutions") or []
    if not rows:
        return ["no resolution row extracted"]
    for r in rows:
        num = str(r.get("resolution_number") or "").strip()
        m = _RES_NUM_RE.match(num)
        if not m:
            problems.append(f"resolution_number {num!r} is not of the form N-YYYY")
            continue
        seq, year = num.split("-")
        if seq == year:
            problems.append(f"resolution_number {num!r} has sequence equal to year (impossible)")
    votes = extracted.get("votes") or []
    if len(votes) > _MAX_VOTES:
        problems.append(f"implausible vote count: {len(votes)}")
    return problems


_VALIDATORS = {
    "resolution": _validate_resolution,
}


def validate_extraction(doc_type_name: str, extracted: dict, profile=None) -> list[str]:
    """Return a list of problem strings for the extracted data; [] means valid.
    Unknown document types have no validator and always pass ([])."""
    validator = _VALIDATORS.get(doc_type_name)
    if validator is None:
        return []
    return validator(extracted or {})
