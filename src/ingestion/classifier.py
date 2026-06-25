"""
Content type classification for chunks.
Uses rule-based logic first; falls back to an LLM call for ambiguous cases (~30% of chunks).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import anthropic

from src.config import Settings, get_settings
from src.models import CONTENT_TYPES

logger = logging.getLogger(__name__)

# Keywords that strongly suggest org/people data
_ORG_KEYWORDS = re.compile(
    r"\b(manages?|director|manages?|reports\s+to|led\s+by|superintendent|"
    r"commissioner|chief|captain|lieutenant|coordinator|administrator|officer|"
    r"supervisor|president|vice\s+president)\b",
    re.IGNORECASE,
)

_GRANT_KEYWORDS = re.compile(
    r"\b(grant|award|funding|reimbursement|federal|state\s+funds?)\b",
    re.IGNORECASE,
)

_BUDGET_KEYWORDS = re.compile(
    r"\b(budget|expenditure|expended|appropriation|encumbrance|"
    r"account\s+number|revised\s+budget|ytd|year[\s-]to[\s-]date)\b",
    re.IGNORECASE,
)

_VACANCY_KEYWORDS = re.compile(
    r"\b(vacancy|vacancies|vacant|position|hire|hiring|unfilled|open\s+position)\b",
    re.IGNORECASE,
)

_PROJECT_KEYWORDS = re.compile(
    r"\b(project|capital|construction|renovation|infrastructure|phase\s+\d|bid|contract)\b",
    re.IGNORECASE,
)


def classify_chunk(
    chunk_dict: dict,
    element_type: str,
    client: Optional[anthropic.Anthropic] = None,
    settings: Optional[Settings] = None,
) -> str:
    """
    Classify a chunk's content type.

    Returns one of: narrative, table, metrics, org_data, project, header

    Rule-based classification is applied first; ambiguous cases use an LLM call.
    """
    text = chunk_dict.get("text", "")
    cfg = settings or get_settings()

    # --- Rule-based classification ---
    result = _rule_based(text, element_type)
    if result is not None:
        return result

    # --- LLM fallback for ambiguous chunks ---
    llm = client or anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    return _llm_classify(text, llm, cfg)


def classify_batch(
    chunk_dicts: list[dict],
    element_types: list[str],
    settings: Optional[Settings] = None,
) -> list[str]:
    """Classify a batch of chunks, reusing the same LLM client."""
    cfg = settings or get_settings()
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    return [
        classify_chunk(c, et, client=client, settings=cfg)
        for c, et in zip(chunk_dicts, element_types)
    ]


def _rule_based(text: str, element_type: str) -> Optional[str]:
    """Return a classification from rules alone, or None if ambiguous."""
    # Unstructured element type overrides
    if element_type == "Table":
        return "table"
    if element_type in ("Title", "Header"):
        return "header"
    if element_type == "OrgData":
        return "org_data"

    # Numeric ratio check (metrics)
    numeric_ratio = _numeric_ratio(text)
    if numeric_ratio > 0.60:
        return "metrics"

    # Budget/expenditure tables (usually tables with account numbers)
    if _BUDGET_KEYWORDS.search(text) and numeric_ratio > 0.15:
        return "table"

    # Org data
    if _ORG_KEYWORDS.search(text) and len(text) < 600:
        return "org_data"

    # Grants — must route to SQL so the extractor can capture them
    if _GRANT_KEYWORDS.search(text) and not _ORG_KEYWORDS.search(text):
        return "table"

    # Vacancy — must route to SQL so the extractor can capture them
    if _VACANCY_KEYWORDS.search(text):
        return "table"

    # Project / capital work
    if _PROJECT_KEYWORDS.search(text) and len(text) > 100:
        return "project"

    # Pure narrative — long text, no numeric content
    if numeric_ratio < 0.05 and len(text) > 150:
        return "narrative"

    return None  # ambiguous → LLM fallback


def _numeric_ratio(text: str) -> float:
    """Fraction of non-whitespace characters that are digits or currency markers."""
    if not text:
        return 0.0
    cleaned = re.sub(r"\s", "", text)
    numeric_chars = len(re.findall(r"[\d,$%.]", cleaned))
    return numeric_chars / len(cleaned)


_LLM_CLASSIFY_PROMPT = """Classify this text chunk from a city government quarterly report.

Valid categories:
- narrative: descriptive text, summaries, background, goals, community engagement
- table: budget table, expenditure table, grant information, vacancy/hiring updates, structured grid of data
- metrics: counts, statistics, performance numbers (inspections, tonnage, call volume)
- org_data: people names, titles, reporting relationships, org chart content
- project: capital project descriptions, construction updates, special project details
- header: section title or heading with little body content

Text chunk:
---
{text}
---

Reply with ONLY the category name, nothing else."""


def _llm_classify(text: str, client: anthropic.Anthropic, cfg: Settings) -> str:
    try:
        msg = client.messages.create(
            model=cfg.synthesis_model,
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": _LLM_CLASSIFY_PROMPT.format(text=text[:1000]),
            }],
        )
        result = msg.content[0].text.strip().lower()
        if result in CONTENT_TYPES:
            return result
        logger.warning("LLM returned unexpected content type: %s — defaulting to narrative", result)
        return "narrative"
    except Exception as e:
        logger.error("LLM classification failed: %s — defaulting to narrative", e)
        return "narrative"
