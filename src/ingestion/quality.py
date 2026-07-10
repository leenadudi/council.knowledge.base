"""Readability scoring to detect garbled OCR text (gibberish made of ASCII letters).

The detector's non-ASCII garble check misses bad embedded OCR layers whose text is
ordinary letters in nonsense order. This scores text by how much it looks like real
English government prose, primarily via stopword hit-rate.
"""
from __future__ import annotations

import re
from typing import Optional

from src.config import Settings, get_settings

# High-frequency English + City-government words. Genuine text hits many of these;
# gibberish hits almost none.
_STOPWORDS = frozenset({
    "the", "of", "and", "to", "a", "in", "for", "is", "on", "that", "by", "this",
    "with", "be", "as", "at", "or", "an", "shall", "hereby", "whereas", "resolved",
    "city", "council", "mayor", "department", "agreement", "authorize", "authorized",
    "resolution", "ordinance", "section", "meeting", "member", "vote", "year",
})

_WORD_RE = re.compile(r"[A-Za-z]+")


def text_readability(text: str) -> float:
    """Fraction of word tokens that are real English words, blending a stopword
    hit-rate with a structural (vowel-bearing, plausible-length) check. 0.0–1.0."""
    if not text or not text.strip():
        return 0.0
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return 0.0

    stop_hits = sum(1 for t in tokens if t in _STOPWORDS)
    stop_rate = stop_hits / len(tokens)

    # Structural plausibility: real words carry a vowel and are 2–15 chars.
    def _plausible(t: str) -> bool:
        return 2 <= len(t) <= 15 and any(v in t for v in "aeiou")
    struct_rate = sum(1 for t in tokens if _plausible(t)) / len(tokens)

    # Stopword presence is the strong signal; weight it heavily. Real prose has a
    # stop_rate well above 0.15; gibberish is near zero. Structural plausibility is a
    # weak tiebreaker only — gibberish is full of vowel-bearing tokens too.
    return min(1.0, stop_rate * 3.0) * 0.8 + struct_rate * 0.2


def is_garbled(text: str, settings: Optional[Settings] = None) -> bool:
    """True when text reads as gibberish (below the configured readability floor)."""
    cfg = settings or get_settings()
    return text_readability(text) < cfg.garble_readability_threshold
