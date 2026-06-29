"""
Metadata extraction and tagging.
Parses department, quarter, and year from the source filename following the
naming convention used in the Harrisburg corpus.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


# Filename pattern:
# "Misc. Documents - Quarterly Reports - 2026 - Bureau of Police_Q1 2026.pdf"
_DEPT_RE = re.compile(
    r"- ([^-]+?)_Q(\d) (\d{4})\.pdf$",
    re.IGNORECASE,
)
_QUARTER_RE = re.compile(r"_Q(\d)\s+(\d{4})", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def filename_hint(source_file: str) -> dict:
    """
    Return {department, quarter, year} extracted from the filename.
    Falls back to sensible defaults if the pattern doesn't match.
    """
    name = Path(source_file).name

    # Try the full pattern first
    m = _DEPT_RE.search(name)
    if m:
        raw_dept = m.group(1).strip(" _()")
        quarter = f"Q{m.group(2)}"
        year = int(m.group(3))
        department = _normalize_department(raw_dept)
        return {"department": department, "quarter": quarter, "year": year}

    # Fallback: extract quarter and year separately
    qm = _QUARTER_RE.search(name)
    ym = _YEAR_RE.search(name)
    quarter = f"Q{qm.group(1)}" if qm else "Q?"
    year = int(ym.group(1)) if ym else datetime.utcnow().year

    # Best-effort department from filename
    department = _parse_department_fallback(name)
    return {"department": department, "quarter": quarter, "year": year}


def _normalize_department(raw: str) -> str:
    """Clean up common artifacts in the department name."""
    dept = raw.strip()
    # Remove trailing year/quarter patterns
    dept = re.sub(r"\s+Q\d\s+\d{4}.*$", "", dept, flags=re.IGNORECASE)
    dept = re.sub(r"\s+\d{4}.*$", "", dept)
    dept = dept.strip(" -()")
    return dept or "Unknown Department"


def _parse_department_fallback(name: str) -> str:
    """Extract department name when the primary regex doesn't match."""
    # Strip common prefix patterns
    cleaned = re.sub(r"Misc\.\s+Documents\s+-\s+Quarterly Reports\s+-\s+\d{4}\s+-\s+", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"_Q\d.*$", "", cleaned).strip(" -()")
    return cleaned or "Unknown Department"


def build_chunk_metadata(
    chunk_dict: dict,
    source_file: str,
    chunk_index: int,
    total_chunks: int,
    content_type: str,
    parser_used: str,
    profile,                       # DocumentProfile
    needs_review: bool = False,
) -> dict:
    """Assemble chunk metadata. Type/department/period come from the profile,
    NOT the filename. Filename is retained only as source_file."""
    # period may be "Q1 2026" or a date/year — split out quarter/year best-effort
    quarter, year = _split_period(profile.period)
    return {
        "source_file": source_file,
        "department": profile.department or "Unknown Department",
        "document_type": profile.document_type,
        "quarter": quarter,
        "year": year,
        "section": chunk_dict.get("section", ""),
        "content_type": content_type,
        "page_number": chunk_dict.get("page_number", 1),
        "parser_used": parser_used,
        "ingestion_timestamp": datetime.utcnow().isoformat(),
        "chunk_index": chunk_index,
        "total_chunks_in_doc": total_chunks,
        "needs_review": needs_review,
    }


def _split_period(period: str) -> tuple:
    """Best-effort: pull a quarter (Qn) and a 4-digit year out of a period string."""
    quarter = ""
    year = None
    if period:
        qm = re.search(r"Q([1-4])", period, re.IGNORECASE)
        if qm:
            quarter = f"Q{qm.group(1)}"
        ym = re.search(r"\b(20\d{2})\b", period)
        if ym:
            year = int(ym.group(1))
    return quarter, year
