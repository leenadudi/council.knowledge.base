"""Deterministic, city-agnostic normalizer for legislative lifecycle events.

Turns the free-text `meeting_actions` fields (item_type / item_number / action)
and the doc-derived `status` fields on resolutions/legislation into a small
canonical event vocabulary. Pure keyword rules — no LLM, no city names.

Clause policy: an action string is split into clauses (';' and em/en dashes);
each clause maps to at most a couple of events. A clause matching nothing
becomes `note` context on the preceding event; if the WHOLE string maps to
nothing, parse_events returns [] and the caller records a review flag.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

EVENT_VOCABULARY = frozenset({
    "introduced", "first_reading", "referred_to_committee", "reported_out",
    "amended", "passed", "tabled", "defeated", "withdrawn",
    "signed", "vetoed", "veto_overridden",
})

_TALLY_RE = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")
_COMMITTEE_RE = re.compile(
    r"(?:referred to|moved (?:to|into))\s+(?:the\s+)?(.+?committee)", re.I)
_NUM_PREFIX_RE = re.compile(
    r"^\s*(?:bill|resolution|res\.?|ordinance|ord\.?)?\s*(?:no\.?|#)?\s*", re.I)
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|—|–)\s*")


@dataclass(frozen=True)
class LegislativeEvent:
    event: str
    committee: str = ""
    ayes: int | None = None
    nays: int | None = None
    note: str = ""


def item_kind(item_type: str | None, item_number: str | None = None) -> str | None:
    """Collapse observed item_type variants to 'resolution' | 'ordinance' | None."""
    t = (item_type or "").strip().lower()
    n = (item_number or "").strip().lower()
    if "resolution" in t:
        return "resolution"
    if "ordinance" in t or "bill" in t:
        return "ordinance"
    if n.startswith("bill"):
        return "ordinance"
    return None


def item_key(item_number: str | None) -> str:
    """Canonical identity: 'Bill 3-2026' / 'Res. No. 3-2026' / '3-2026' → '3-2026'."""
    k = _NUM_PREFIX_RE.sub("", (item_number or "").strip())
    return re.sub(r"\s+", " ", k).strip().lower()


def _clause_events(clause: str) -> list[LegislativeEvent]:
    c = clause.strip().rstrip(".")
    low = c.lower()
    if not low or low == "none":
        return []
    m = _TALLY_RE.search(c)
    ayes, nays = (int(m.group(1)), int(m.group(2))) if m else (None, None)

    # Order matters: negations and amendment votes before their substrings.
    if re.search(r"\b(did not pass|failed|defeated)\b", low):
        return [LegislativeEvent("defeated", ayes=ayes, nays=nays)]
    if re.search(r"\bamendment (?:adopted|passed|approved)\b", low):
        return [LegislativeEvent("amended", ayes=ayes, nays=nays)]
    if "withdrawn" in low:
        return [LegislativeEvent("withdrawn")]
    if "tabled" in low:
        return [LegislativeEvent("tabled")]
    if re.search(r"\b(passed|adopted|approved|final passage)\b", low):
        note = ""
        if m:
            trailing = c[m.end():].strip(" ,;—–")
            note = trailing if len(trailing) > 3 else ""
        evs = [LegislativeEvent("passed", ayes=ayes, nays=nays, note=note)]
        if "as amended" in low:
            evs.append(LegislativeEvent("amended"))
        return evs
    if "reported out" in low:
        return [LegislativeEvent("reported_out")]
    cm = _COMMITTEE_RE.search(c)
    if cm or re.search(r"\b(referred to|moved (?:to|into))\b.*\bcommittee\b", low):
        committee = cm.group(1).strip() if cm else ""
        if committee.lower() == "committee":
            committee = ""
        return [LegislativeEvent("referred_to_committee", committee=committee)]
    if "first reading" in low:
        return [LegislativeEvent("first_reading")]
    if re.search(r"\bread\b.*\brecord\b|\bintroduced\b", low):
        return [LegislativeEvent("introduced")]
    return []


def parse_events(item_type: str | None, action: str | None) -> list[LegislativeEvent]:
    events: list[LegislativeEvent] = []
    for clause in _CLAUSE_SPLIT_RE.split((action or "").strip()):
        found = _clause_events(clause)
        if found:
            events.extend(found)
        elif events and clause.strip():
            prev = events[-1]
            joined = f"{prev.note}; {clause.strip()}" if prev.note else clause.strip()
            events[-1] = replace(prev, note=joined)
    return events


def doc_status_events(status: str | None) -> list[str]:
    """Events implied by a document's own status field (legislation/resolutions)."""
    s = (status or "").strip().lower().replace("_", " ")
    if not s:
        return []
    if "veto" in s and ("overrid" in s or "override" in s):
        return ["veto_overridden"]
    if "vetoed" in s or s == "veto":
        return ["vetoed"]
    if "signed" in s or "enacted" in s:
        return ["signed"]
    if s in {"passed", "adopted", "approved"} or "final passage" in s:
        return ["passed"]
    if "amend" in s:
        return ["amended"]
    if "introduc" in s:
        return ["introduced"]
    return []
