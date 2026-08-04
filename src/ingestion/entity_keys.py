"""Deterministic, city-agnostic canonical keys for entity dimensions.

Turns a raw extracted string (department, person, or vendor) into a stable
`canonical_key`: two raw strings that denote the same real entity produce the
same key. The logic is generic US-municipal-naming normalization — it hardcodes
NO city's specific departments, people, or vendors.

Keys MUST match the dashboard's existing canonicalization so the SQL dimension
and the dashboard never disagree:
  department_key <-> DashboardAggregator._dept_key_base (dashboard/aggregator.py)
  vendor_key     <-> dashboard.projects._normalize_party (dashboard/projects.py)
  member_key     wraps ingestion.names.normalize_person_name
The parity tests in tests/ingestion/test_entity_keys.py guard against drift; if
you change the dashboard functions, change these together.
"""
from __future__ import annotations

import re

from src.ingestion.names import normalize_person_name

_DEPT_PREFIXES = ("bureau of ", "department of ", "office of ", "the ")
_VENDOR_SUFFIX_RE = re.compile(
    r"[,\.]?\s*\b(llc|l\.l\.c|inc|incorporated|co|company|corp|corporation|lp|llp|pllc)\b\.?",
    re.I,
)


def department_key(name: str | None) -> str:
    """Canonical key for a department. Mirrors DashboardAggregator._dept_key_base."""
    k = (name or "").strip().lower().replace(" and ", " & ")
    for p in _DEPT_PREFIXES:
        if k.startswith(p):
            k = k[len(p):]
            break
    return re.sub(r"[^a-z0-9&]+", " ", k).strip()


def member_key(name: str | None) -> str:
    """Canonical key for a council member — strips honorifics, then lowercases."""
    return normalize_person_name(name).lower().strip()


def vendor_key(name: str | None) -> str:
    """Canonical key for a vendor/party. Mirrors dashboard.projects._normalize_party."""
    s = (name or "").lower()
    s = _VENDOR_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def infer_dept_type(name: str | None) -> str | None:
    """Best-effort department type from the generic prefix. City-agnostic."""
    if not name:
        return None
    k = name.strip().lower()
    if k.startswith("bureau of"):
        return "bureau"
    if k.startswith("department of"):
        return "department"
    if k.startswith("office of"):
        return "office"
    if "council" in k:
        return "council"
    return "other"
