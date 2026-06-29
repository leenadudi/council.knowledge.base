"""
LLM-as-judge for ingestion quality.

Mirrors the pattern in src/evaluation/evaluator.py:
  - Uses TrackedAnthropic(cfg, call_site="eval.ingestion_judge")
  - Parses JSON by brace-slicing (find first '{' … last '}')
  - NEVER raises — returns a safe default dict on any failure

Usage
-----
    from src.evaluation.ingestion_judge import judge_extraction

    verdict = judge_extraction(
        source_text=raw_text,
        extracted=extracted_dict,
        expected_notes="Should capture the $40,000 award to the vendor.",
    )
    # verdict == {"score": 1-5, "complete": bool, "hallucinated": bool, "reasoning": str}
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.config import Settings, get_settings
from src.llm.client import TrackedAnthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT = """You audit structured data extracted from a city government document.
Given the SOURCE TEXT and the EXTRACTED JSON, judge the extraction.

Score 1-5 (5 = complete and faithful). Flag hallucination if any extracted value is
not supported by the source. Consider these expectations: {notes}

SOURCE TEXT:
---
{source}
---
EXTRACTED JSON:
{extracted}

Return ONLY JSON: {{"score": 1-5, "complete": true|false, "hallucinated": true|false, "reasoning": "..."}}"""

# ---------------------------------------------------------------------------
# Safe default returned on any parse or LLM failure
# ---------------------------------------------------------------------------

_SAFE_DEFAULT: dict[str, Any] = {
    "score": 0,
    "complete": False,
    "hallucinated": True,
    "reasoning": "unparseable",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def judge_extraction(
    source_text: str,
    extracted: dict[str, Any],
    expected_notes: str = "",
    settings: Optional[Settings] = None,
    client: Optional[TrackedAnthropic] = None,
) -> dict[str, Any]:
    """
    Ask an LLM to judge the quality of an extraction against the source text.

    Parameters
    ----------
    source_text    : The raw document text that was parsed.
    extracted      : The dict returned by SQLExtractor.extract_for_type.
    expected_notes : Free-text guidance on what the judge should look for
                     (comes from the fixture's ``judge_notes`` field).
    settings       : Optional Settings override (uses get_settings() if None).
    client         : Optional TrackedAnthropic override (constructed from
                     settings if None). Accepts any object with a
                     ``.messages.create(**kwargs)`` method returning an object
                     whose ``.content[0].text`` is a string — used in tests
                     to inject a fake client without an API key.

    Returns
    -------
    dict with keys: score (int 0-5), complete (bool), hallucinated (bool),
    reasoning (str). Never raises — returns _SAFE_DEFAULT on any failure.
    """
    cfg = settings or get_settings()
    llm = client or TrackedAnthropic(cfg, call_site="eval.ingestion_judge")

    prompt = _PROMPT.format(
        notes=expected_notes,
        source=source_text[:6000],
        extracted=json.dumps(extracted)[:4000],
    )

    try:
        msg = llm.messages.create(
            model=cfg.synthesis_model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

        # Brace-slice: keep only the outermost JSON object
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError(f"No JSON object found in response: {raw[:120]!r}")
        raw = raw[start: end + 1]

        return json.loads(raw)

    except Exception as exc:
        logger.warning("ingestion_judge parse failed: %s", exc)
        return dict(_SAFE_DEFAULT)  # return a fresh copy each time
