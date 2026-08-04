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


# Grant names arrive in many spellings of the same award: acronym-first
# ("CDBG (Community Development Block Grant)"), expansion-first
# ("Community Development Block Grant (CDBG)"), and with trailing noise
# ("... (CDBG) Program", "... (ARPA) funds", "... Program Year 2025").
# grant_key unifies these on the acronym while keeping genuinely distinct
# grants apart (CDBG vs CDBG-CV, HOME vs HOME-ARP) and preserving " - "
# sub-award descriptors ("CDBG - Broad Street Market").
_GRANT_NOISE_TOKENS = {"program", "programs", "project", "projects", "grant",
                       "grants", "fund", "funds", "funding", "year"}
_GRANT_YEAR_RE = re.compile(r"(19|20)\d{2}(-\d{2,4})?")
_GRANT_DESC_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_GRANT_PAREN_RE = re.compile(r"^([^(]*)\(([^)]*)\)(.*)$")

# statuses that count toward row-completeness when picking a dedupe survivor
ACTIVE_GRANT_STATUSES = ("active", "in_progress", "open", "pending", "awarded")


def _alnum(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _core_tokens(s: str) -> list[str]:
    """Whitespace tokens with trailing noise words / bare years removed."""
    toks = s.split()
    while toks and (_alnum(toks[-1]) in _GRANT_NOISE_TOKENS
                    or _GRANT_YEAR_RE.fullmatch(toks[-1])):
        toks.pop()
    return toks


def grant_key(name: str | None) -> str:
    """Canonical key for a grant/award name (see comment block above)."""
    s = (name or "").strip().lower()
    if not s:
        return ""
    parts = _GRANT_DESC_SPLIT_RE.split(s, maxsplit=1)
    base, desc = parts[0], parts[1] if len(parts) > 1 else ""
    m = _GRANT_PAREN_RE.match(base)
    if m:
        pre, inner, post = (t.strip() for t in m.groups())
        pre_toks = _core_tokens(pre)
        inner_toks = _core_tokens(inner)
        if len(pre_toks) == 1:
            # acronym before the parens: "CDBG (…)", "CDBG-CV Grant (…)"
            core = _alnum(pre_toks[0])
        elif len(inner_toks) == 1 and pre_toks:
            # acronym inside the parens: "Community Development Block Grant (CDBG)"
            core = _alnum(inner_toks[0])
        else:
            # neither side is an acronym — the parenthetical is a distinguishing
            # qualifier ("Park Improvement Grant (Reservoir Park)"), keep both
            core = (" ".join(filter(None, [_alnum(" ".join(pre_toks)),
                                           _alnum(" ".join(inner_toks))]))
                    or _alnum(pre) or _alnum(inner))
        post_core = _alnum(" ".join(_core_tokens(post)))
        core = f"{core} {post_core}".strip()
    else:
        core = _alnum(" ".join(_core_tokens(base))) or _alnum(base)
    desc_core = _alnum(" ".join(_core_tokens(desc))) or _alnum(desc)
    return " ".join(filter(None, [core, desc_core]))


def dedupe_grant_rows(rows: list[dict], dept_key=department_key) -> list[dict]:
    """Collapse rows that describe the same grant — same grant_key + same
    department key — keeping the most complete row. Blank-name rows can't be
    keyed and pass through untouched. dept_key is injectable so the dashboard
    can apply its alias-aware department canonicalization."""
    def score(r) -> int:
        s = sum(1 for f in ("amount", "start_date", "end_date") if r.get(f) is not None)
        if (r.get("grant_number") or "").strip():
            s += 1
        if (r.get("status") or "").strip().lower() in ACTIVE_GRANT_STATUSES:
            s += 1
        return s

    best: dict = {}
    blanks: list = []
    for r in rows:
        gk = grant_key(r.get("grant_name"))
        if not gk:
            blanks.append(r)
            continue
        key = (gk, dept_key(r.get("department") or ""))
        if key not in best or score(r) > score(best[key]):
            best[key] = r
    return list(best.values()) + blanks


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
