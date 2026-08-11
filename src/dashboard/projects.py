"""Live-derived Projects layer over grants + resolutions (deterministic, no LLM).

A "project" is a synthesis over already-extracted data, NOT a new table: every
grant is a grant-funded project, and each resolution is classified by type and
surfaced as an initiative (land development, contract, grant action) or bucketed
as administrative (budget/appointment). Assembled fresh every build, so newly
ingested resolutions appear automatically. See
docs/superpowers/specs/2026-07-09-projects-phase1-design.md.
"""
from __future__ import annotations

import datetime
import logging
import re
from collections import Counter
from datetime import timezone

from src.dashboard.aggregator import DashboardAggregator  # noqa: F401 (kept for parity/reuse)
from src.ingestion.entity_keys import grant_key


def _period_label(year, quarter) -> str:
    return f"{(quarter or '').strip()} {year}".strip() if year else (quarter or "").strip()


def _period_tuple(year, quarter):
    return (int(year) if year is not None else 0, (quarter or "").strip())


def _grant_completeness(r: dict) -> tuple:
    """Rank a grant row by how much it pins down — picks which row in a rolled-up
    group supplies the award amount and dates."""
    filled = sum(1 for f in ("amount", "start_date", "end_date") if r.get(f) is not None)
    if (r.get("grant_number") or "").strip():
        filled += 1
    return (filled, float(r["amount"]) if r.get("amount") is not None else 0.0)

logger = logging.getLogger(__name__)

_EXPIRING_DAYS = 120

# Resolution title -> type. First match wins; order matters (land dev before grant).
_TYPE_RULES = [
    ("land_development", re.compile(r"land development|subdivision|zoning|plat|rezon", re.I)),
    ("grant_action",     re.compile(r"grant", re.I)),
    ("contract",         re.compile(r"agreement|contract|professional services|purchase|negotiat|lease", re.I)),
    ("budget",           re.compile(r"budget|appropriat|millage|\btax\b", re.I)),
    ("appointment",      re.compile(r"appoint|reappoint|resign|confirm", re.I)),
]
_ADMIN_TYPES = {"budget", "appointment"}

_GRANT_STATUS = {"active": "Active", "awarded": "Awarded", "applied": "Proposed",
                 "pending": "Proposed", "closed": "Closed"}
_RES_STATUS_RULES = [
    (re.compile(r"tabl", re.I), "Stalled"),
    (re.compile(r"fail|defeat|reject", re.I), "Closed"),
    (re.compile(r"pass|adopt|approv|ratif", re.I), "Active"),
]


def classify_resolution(title: str) -> str:
    t = title or ""
    for name, rx in _TYPE_RULES:
        if rx.search(t):
            return name
    return "other"


def normalize_grant_status(s: str) -> str:
    return _GRANT_STATUS.get((s or "").strip().lower(), "Active")


def grant_status_display(status: str, live: bool) -> str:
    """Status label for the grant rollup. Recognized lifecycle statuses map as
    usual; free-text or blank statuses (most of this data) fall back to live-ness
    so they don't all read as 'Active' and inflate the active count."""
    return _GRANT_STATUS.get((status or "").strip().lower()) or ("Active" if live else "Closed")


def normalize_resolution_status(s: str) -> str:
    for rx, val in _RES_STATUS_RULES:
        if rx.search(s or ""):
            return val
    return "Proposed"


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else d


_PARTY_SUFFIX_RE = re.compile(
    r"[,\.]?\s*\b(llc|l\.l\.c|inc|incorporated|co|company|corp|corporation|lp|llp|pllc)\b\.?",
    re.I,
)


def _normalize_party(vendor: str) -> str:
    """Canonicalize a vendor/party string so trivial variants group together:
    lowercase, drop trailing corporate suffixes/punctuation, collapse whitespace."""
    s = (vendor or "").lower()
    s = _PARTY_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


class Projects:
    """Assembles the live Projects layer from grants + resolutions."""

    def __init__(self, sql_store, now: datetime.datetime | None = None):
        self.sql = sql_store
        self.now = now or datetime.datetime.now(timezone.utc)

    def build(self) -> dict:
        today = self.now.date()
        horizon = today + datetime.timedelta(days=_EXPIRING_DAYS)
        with self.sql.cursor() as cur:
            # Period per grant row comes from its source document (the grants table
            # has no quarter/year); LATERAL keeps it one row per grant.
            cur.execute("SELECT g.id, g.grant_name, g.department, g.amount, g.start_date, "
                        "g.end_date, g.status, g.source_file, d.quarter, d.year "
                        "FROM grants g LEFT JOIN LATERAL ("
                        "SELECT quarter, year FROM documents WHERE source_file = g.source_file "
                        "ORDER BY year DESC NULLS LAST, quarter DESC LIMIT 1) d ON TRUE")
            grant_rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT resolution_number, title, vendor, amount, department, "
                        "adopted_date, status, source_file FROM resolutions")
            res_rows = [dict(r) for r in cur.fetchall()]

        # De-duplicate resolutions by number (defensive — same resolution could be
        # extracted from more than one document), keeping the first seen.
        seen_res: set = set()
        deduped_res = []
        for r in res_rows:
            rn = (r.get("resolution_number") or "").strip()
            if rn and rn in seen_res:
                continue
            if rn:
                seen_res.add(rn)
            deduped_res.append(r)
        res_rows = deduped_res

        projects: list[dict] = []
        administrative: list[dict] = []

        # Roll grant rows up by canonical grant key: one project per real grant,
        # carrying its quarter-by-quarter status trail. The latest report drives the
        # status pill and display; the most complete row supplies the award amount
        # and dates. Same grant re-extracted across reports (or under name variants,
        # even in a renamed department) collapses into one tracked project.
        grant_groups: dict = {}
        for r in grant_rows:
            gk = grant_key(r.get("grant_name"))
            if not gk:
                continue  # blank / unkeyable name — nothing to track
            grant_groups.setdefault(gk, []).append(r)

        for grp in grant_groups.values():
            grp.sort(key=lambda r: _period_tuple(r.get("year"), r.get("quarter")))
            latest = grp[-1]
            richest = max(grp, key=_grant_completeness)
            amount = float(richest["amount"]) if richest.get("amount") is not None else None
            status = normalize_grant_status(latest.get("status"))
            end = richest.get("end_date")
            expiring = bool(end and hasattr(end, "toordinal") and today <= end <= horizon)
            projects.append({
                "id": f"grant-{latest['id']}", "source": "grant", "type": "grant",
                "title": (latest.get("grant_name") or "").strip(),
                "department": latest.get("department"),
                "party": None, "amount": amount, "status": status,
                "date": _iso(richest.get("start_date")), "end_date": _iso(end),
                "source_file": latest.get("source_file"), "resolution_number": None,
                "attention": expiring or status == "Stalled",
                "group_key": None,
                "history": [{"period": _period_label(r.get("year"), r.get("quarter")),
                             "status": r.get("status")} for r in grp],
                "latest_period": _period_label(latest.get("year"), latest.get("quarter")),
            })

        for r in res_rows:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            typ = classify_resolution(title)
            amount = float(r["amount"]) if r.get("amount") is not None else None
            status = normalize_resolution_status(r.get("status"))
            party = r.get("vendor") or None
            rec = {
                "id": f"res-{r.get('resolution_number')}", "source": "resolution", "type": typ,
                "title": title, "department": r.get("department"),
                "party": party, "amount": amount, "status": status,
                "date": _iso(r.get("adopted_date")), "end_date": None,
                "source_file": r.get("source_file"), "resolution_number": r.get("resolution_number"),
                "attention": status == "Stalled",
                "group_key": (f"{_normalize_party(party)}|{typ}" if party else None),
            }
            (administrative if typ in _ADMIN_TYPES else projects).append(rec)

        # attention first, then largest funding, then title
        projects.sort(key=lambda p: (0 if p["attention"] else 1, -(p["amount"] or 0), p["title"].lower()))
        administrative.sort(key=lambda p: p["title"].lower())

        counts = {
            "active": sum(1 for p in projects if p["status"] == "Active"),
            "attention": sum(1 for p in projects if p["attention"]),
            "by_type": dict(Counter(p["type"] for p in projects)),
        }
        funding = sum(p["amount"] or 0 for p in projects if p["status"] in ("Active", "Awarded", "Proposed"))
        return {"projects": projects, "administrative": administrative,
                "counts": counts, "funding_in_flight": funding}
