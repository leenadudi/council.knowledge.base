"""Canonicalize person names so graph/SQL don't fragment one person into many."""
from __future__ import annotations

import re

# Leading honorifics/titles to strip (longest/most-specific first), case-insensitive.
_TITLE_RE = re.compile(
    r"^(?:"
    r"council\s*member|councilmember|councilman|councilwoman|council\s*president|"
    r"vice\s+president|president|"
    r"mrs|mr|ms|dr|hon|honorable"
    r")\.?\s+",
    re.IGNORECASE,
)


def normalize_person_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\s+", " ", str(name)).strip()
    # Strip leading titles repeatedly (e.g. "Hon. Council Member X").
    prev = None
    while cleaned and cleaned != prev:
        prev = cleaned
        cleaned = _TITLE_RE.sub("", cleaned).strip()
    return cleaned.title()
