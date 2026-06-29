"""
SQL extraction: given a batch of table/metrics chunks, calls the LLM to extract
structured rows for the expenditures, metrics, grants, and vacancies tables.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from src.llm.client import TrackedAnthropic

from src.config import Settings, get_settings
from src.models import Chunk

logger = logging.getLogger(__name__)

_SQL_EXTRACTION_PROMPT = """You are a precise data extractor for City of Harrisburg government quarterly reports.

Source document: department "{department}", quarter "{quarter}", year {year}.

EXTRACTION RULES — read carefully before extracting anything:

1. For every value you extract, you MUST include a "source_text" field with the exact verbatim quote from the text that supports it. If you cannot find a direct quote, do not extract the value.
2. Rate your confidence as "high", "medium", or "low" for each row. Only extract rows with "high" or "medium" confidence.
3. Dollar amounts must come from a clearly labeled total or award figure — NOT from expenditure sub-lines, budget allocations, or spending breakdowns.
4. For grants: "amount" is the TOTAL GRANT AWARD to the City, not a line item of how it was spent.
5. For metrics: only extract explicitly stated counts or totals — do not infer or calculate.
6. For vacancies: "status" must be exactly "open" or "filled" based on what the text states.
7. If the text is garbled, incomplete, or ambiguous, set confidence to "low" and omit the row.
8. Do NOT extract the same fact twice across multiple chunks.

Extract only the following types where clearly present:

1. Budget/expenditure rows (only from structured Munis-style budget tables with account numbers):
{{
  "expenditures": [
    {{
      "account_number": "...",
      "line_item": "...",
      "sub_department": "...",
      "revised_budget": 0.00,
      "ytd_expended": 0.00,
      "source_text": "exact quote from text",
      "confidence": "high|medium|low",
      "department": "{department}",
      "quarter": "{quarter}",
      "year": {year}
    }}
  ]
}}

2. Performance metrics (explicitly stated counts, totals, or rates — not inferred):
{{
  "metrics": [
    {{
      "metric_name": "...",
      "metric_value": 0.0,
      "metric_unit": "count|dollars|percent|hours|other short unit",
      "source_text": "exact quote from text",
      "confidence": "high|medium|low",
      "department": "{department}",
      "quarter": "{quarter}",
      "year": {year}
    }}
  ]
}}

3. Grant information (total award amounts only, not spending breakdowns):
{{
  "grants": [
    {{
      "grant_name": "...",
      "grant_number": "...",
      "amount": 0.00,
      "start_date": null,
      "end_date": null,
      "status": "active|closed|pending|in_progress",
      "source_text": "exact quote from text",
      "confidence": "high|medium|low",
      "department": "{department}"
    }}
  ]
}}

4. Vacancy information:
{{
  "vacancies": [
    {{
      "position_title": "...",
      "status": "open|filled",
      "source_text": "exact quote from text",
      "confidence": "high|medium|low",
      "department": "{department}",
      "quarter": "{quarter}",
      "year": {year}
    }}
  ]
}}

Return a single JSON object with only keys that have high/medium confidence rows (omit empty lists and all low-confidence rows).
Dollar amounts: plain numbers, no $ or commas. Dates: YYYY-MM-DD or null.

Text to extract from:
---
{text}
---
"""


class SQLExtractor:
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="ingestion.sql_extractor")

    def extract_batch(self, chunks: list[Chunk]) -> dict[str, list[dict[str, Any]]]:
        """
        Process a batch of chunks (up to EXTRACTION_BATCH_SIZE) in a single LLM call.
        Returns {expenditures: [...], metrics: [...], grants: [...], vacancies: [...]}
        """
        if not chunks:
            return {}

        # Group by department/quarter/year so we can provide proper context
        # For a mixed batch, use the first chunk's metadata
        meta = chunks[0].metadata
        combined_text = "\n\n---CHUNK BOUNDARY---\n\n".join(c.text for c in chunks)

        prompt = _SQL_EXTRACTION_PROMPT.format(
            department=meta.department,
            quarter=meta.quarter,
            year=meta.year,
            text=combined_text[:6000],
        )

        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text
            return _parse_extraction_response(raw)
        except Exception as e:
            logger.error("SQL extraction failed for batch of %d chunks: %s", len(chunks), e)
            return {}

    def extract_for_type(self, chunks, doc_type) -> dict[str, list[dict[str, Any]]]:
        """
        Extract structured data against the document type's Pydantic extraction_schema.
        Returns only the keys in doc_type.sql_targets, keeping high/medium-confidence rows.
        On any exception, logs a warning and returns {}.
        """
        if not chunks or doc_type is None or doc_type.extraction_schema is None:
            return {}
        text = "\n\n---\n\n".join(c.text for c in chunks)
        schema_json = json.dumps(doc_type.extraction_schema.model_json_schema())
        prompt = (
            f"You are a precise data extractor for City of Harrisburg '{doc_type.name}' documents.\n"
            f"Extract structured data matching THIS JSON schema (return an object with these keys):\n"
            f"{schema_json}\n\n"
            "Rules: include a verbatim 'source_text' for every row; set 'confidence' to high|medium|low "
            "and omit low-confidence rows; dollar amounts as plain numbers; dates YYYY-MM-DD or null. "
            "Return ONLY the JSON object.\n\nText:\n---\n" + text + "\n---"
        )
        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            validated = doc_type.extraction_schema.model_validate_json(raw)
            data = validated.model_dump()
            # keep only the doc_type's declared sql_targets, drop low-confidence rows
            return {
                k: [r for r in v if r.get("confidence") in ("high", "medium")]
                for k, v in data.items()
                if k in doc_type.sql_targets and v
            }
        except Exception as e:
            logger.warning("schema-driven extraction failed for %s: %s", doc_type.name, e)
            return {}

    def extract_chunks_batched(
        self, chunks: list[Chunk]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Extract from all chunks, sending them in batches of EXTRACTION_BATCH_SIZE.
        Merges all results.
        """
        all_results: dict[str, list] = {}
        batch_size = self.cfg.extraction_batch_size

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            result = self.extract_batch(batch)
            for key, rows in result.items():
                all_results.setdefault(key, []).extend(rows)

        return all_results


def _parse_extraction_response(raw: str) -> dict[str, list[dict]]:
    """Parse and validate the JSON returned by the extraction LLM call."""
    # Strip markdown code fences
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    json_str = match.group(1) if match else raw

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("SQL extractor returned non-JSON: %s", raw[:200])
        return {}

    result = {}
    for key in ("expenditures", "metrics", "grants", "vacancies"):
        rows = data.get(key, [])
        if isinstance(rows, list) and rows:
            result[key] = _sanitize_rows(rows, key)

    return result


_VARCHAR_LIMITS: dict[str, int] = {
    "metric_unit": 200,
    "status": 200,
    "quarter": 5,
    "account_number": 50,
    "document_type": 50,
    "parser_used": 50,
}

_EXTRACTION_ONLY_FIELDS = {"source_text", "confidence"}


def _sanitize_rows(rows: list[dict], table: str) -> list[dict]:
    """
    Clean up extracted rows:
    - Drop low-confidence rows
    - Log source_text for auditability, then strip it before DB insert
    - Strip currency symbols, coerce numeric types
    - Enforce VARCHAR length limits
    """
    clean = []
    for row in rows:
        confidence = row.get("confidence", "high")
        if confidence == "low":
            logger.info(
                "Skipping low-confidence %s row: %s (source: %s)",
                table, row.get("metric_name") or row.get("grant_name") or row.get("line_item") or row.get("position_title"),
                row.get("source_text", "")[:120],
            )
            continue

        if row.get("source_text"):
            logger.debug("Extracting %s row from: %s", table, row["source_text"][:120])

        r = {}
        for k, v in row.items():
            if k in _EXTRACTION_ONLY_FIELDS:
                continue
            if isinstance(v, str):
                if any(term in k for term in ("budget", "expended", "amount", "value")):
                    try:
                        v = float(re.sub(r"[$,]", "", v)) if v else None
                    except (ValueError, TypeError):
                        pass
                else:
                    limit = _VARCHAR_LIMITS.get(k)
                    if limit and len(v) > limit:
                        logger.warning("Truncating %s.%s from %d to %d chars", table, k, len(v), limit)
                        v = v[:limit]
            r[k] = v
        clean.append(r)
    return clean
