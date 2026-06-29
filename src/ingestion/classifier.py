"""
Content type classification for chunks.
Uses rule-based logic first; falls back to an LLM call for ambiguous cases (~30% of chunks).
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from src.llm.client import TrackedAnthropic

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


def _make_llm(cfg: Settings) -> TrackedAnthropic:
    return TrackedAnthropic(cfg, call_site="ingestion.classifier")


def _validate_against_vocab(result: str, vocab) -> str:
    """Validate a classification result against an allowed vocabulary.

    If vocab is provided, result must be in vocab (falls back to vocab[0]).
    If vocab is None, result must be in CONTENT_TYPES (falls back to "narrative").
    """
    result = (result or "").strip().lower()
    allowed = vocab if vocab else list(CONTENT_TYPES)
    if result in allowed:
        return result
    return allowed[0] if vocab else "narrative"


def classify_chunk(
    chunk_dict: dict,
    element_type: str,
    client: Optional[TrackedAnthropic] = None,
    settings: Optional[Settings] = None,
    vocab: Optional[list] = None,
) -> str:
    """
    Classify a chunk's content type.

    Returns one of: narrative, table, metrics, org_data, project, header
    (or a vocab-specific category when vocab is provided).

    When vocab is None: rule-based classification is applied first; ambiguous
    cases use an LLM call.
    When vocab is provided: skip rule-based (quarterly-report-specific) and go
    straight to the LLM with the given vocabulary.
    """
    text = chunk_dict.get("text", "")
    cfg = settings or get_settings()

    llm = client or _make_llm(cfg)

    if vocab is not None:
        # Vocab supplied — skip rule-based categories and use LLM directly
        return _llm_classify(text, llm, cfg, vocab=vocab)

    # --- Rule-based classification (vocab=None path, unchanged) ---
    result = _rule_based(text, element_type)
    if result is not None:
        return result

    # --- LLM fallback for ambiguous chunks ---
    return _llm_classify(text, llm, cfg)


def classify_batch(
    chunk_dicts: list[dict],
    element_types: list[str],
    settings: Optional[Settings] = None,
    vocab: Optional[list] = None,
) -> list[str]:
    """Classify a batch of chunks concurrently, reusing the same LLM client.

    Uses a ThreadPoolExecutor capped at cfg.ingest_workers to run per-chunk
    classify_chunk calls in parallel while preserving input order.
    Empty input returns [] immediately.
    """
    if not chunk_dicts:
        return []
    cfg = settings or get_settings()
    client = _make_llm(cfg)
    workers = max(1, min(cfg.ingest_workers, len(chunk_dicts)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(
            lambda pair: classify_chunk(pair[0], pair[1], client=client, settings=cfg, vocab=vocab),
            list(zip(chunk_dicts, element_types)),
        ))
    return results


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


_LLM_CLASSIFY_PROMPT_HEADER = """Classify this text chunk from a city government document.

Valid categories:
{categories}

Text chunk:
---
{text}
---

Reply with ONLY the category name, nothing else."""

# Default category descriptions for the standard CONTENT_TYPES vocabulary
_DEFAULT_CATEGORY_LINES = (
    "- narrative: descriptive text, summaries, background, goals, community engagement\n"
    "- table: budget table, expenditure table, grant information, vacancy/hiring updates, structured grid of data\n"
    "- metrics: counts, statistics, performance numbers (inspections, tonnage, call volume)\n"
    "- org_data: people names, titles, reporting relationships, org chart content\n"
    "- project: capital project descriptions, construction updates, special project details\n"
    "- header: section title or heading with little body content"
)


def _llm_classify(
    text: str,
    client: TrackedAnthropic,
    cfg: Settings,
    vocab: Optional[list] = None,
) -> str:
    # Build the category list shown to the LLM
    if vocab:
        categories = "\n".join(f"- {v}" for v in vocab)
        default_fallback = vocab[0]
    else:
        categories = _DEFAULT_CATEGORY_LINES
        default_fallback = "narrative"

    prompt = _LLM_CLASSIFY_PROMPT_HEADER.format(
        categories=categories,
        text=text[:1000],
    )

    try:
        msg = client.messages.create(
            model=cfg.synthesis_model,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        result = msg.content[0].text.strip().lower()
        return _validate_against_vocab(result, vocab)
    except Exception as e:
        logger.error("LLM classification failed: %s — defaulting to %s", e, default_fallback)
        return default_fallback
