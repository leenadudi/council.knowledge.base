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

    def extract_for_type(self, chunks, doc_type, profile=None) -> dict[str, list[dict[str, Any]]]:
        """
        Extract structured data against the document type's Pydantic extraction_schema.
        Returns only the keys in doc_type.sql_targets, keeping high/medium-confidence rows.
        On any exception, logs a warning and returns {}.

        When doc_type.anchor_field is set and profile supplies the matching identifier,
        an anchor block is injected into the prompt and a deterministic guard collapses
        the primary table to exactly one row keyed to that identifier.
        """
        if not chunks or doc_type is None or doc_type.extraction_schema is None:
            return {}

        try:
            # --- anchor setup ---
            anchor_field = getattr(doc_type, "anchor_field", None)
            anchor_value = None
            anchor_block = ""
            if anchor_field and profile is not None:
                anchor_value = (profile.identifying_ids or {}).get(anchor_field)
                if anchor_value:
                    anchor_block = (
                        f"\nThis document is a SINGLE {doc_type.name}. Its {anchor_field} is "
                        f"\"{anchor_value}\" (department: {profile.department or 'unknown'}, "
                        f"period: {profile.period or 'unknown'}). Extract exactly ONE primary record "
                        f"for THIS document plus its vote record. Do NOT invent additional "
                        f"{doc_type.name}s or split it into multiple records.\n"
                    )

            text = "\n\n---\n\n".join(c.text for c in chunks)
            schema_json = json.dumps(doc_type.extraction_schema.model_json_schema())
            prompt = (
                f"You are a precise data extractor for City of Harrisburg '{doc_type.name}' documents.\n"
                + anchor_block
                + f"Extract structured data matching THIS JSON schema (return an object with these keys):\n"
                f"{schema_json}\n\n"
                "Rules: include a verbatim 'source_text' for every row; set 'confidence' to high|medium|low "
                "and omit low-confidence rows; dollar amounts as plain numbers; dates YYYY-MM-DD or null. "
                "Return ONLY the JSON object.\n\nText:\n---\n" + text + "\n---"
            )
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                # 8000 (not 2000): action-heavy docs like minutes emit long JSON;
                # a truncated response (stop_reason=max_tokens) is invalid JSON and
                # silently yields zero rows. Ceiling only — short docs pay for what they use.
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            validated = doc_type.extraction_schema.model_validate_json(raw)
            data = validated.model_dump()
            # keep only the doc_type's declared sql_targets, drop low-confidence rows
            result = {
                # missing-confidence rows are dropped intentionally: schemas mandate
                # "confidence"; this differs from _sanitize_rows which defaults to "high"
                k: [r for r in v if r.get("confidence") in ("high", "medium")]
                for k, v in data.items()
                if k in doc_type.sql_targets and v
            }
            # deterministic anchor guard: collapse the primary table to exactly one
            # row keyed to the profiler's identifier (kills hallucinated duplicates).
            if anchor_value and doc_type.sql_targets:
                primary = doc_type.sql_targets[0]
                rows = result.get(primary) or []
                if rows:
                    match = next(
                        (r for r in rows if str(r.get(anchor_field)) == str(anchor_value)),
                        rows[0],
                    )
                    match[anchor_field] = anchor_value
                    result[primary] = [match]
            return result
        except Exception as e:
            logger.warning("schema-driven extraction failed for %s: %s", doc_type.name, e)
            return {}

    def extract_goals(self, texts: list[str], department: str = "",
                      quarter: str = "", year: Optional[int] = None) -> list[dict[str, Any]]:
        """Extract department goals from a quarterly report's 'Annual Goals' section.
        `texts` are the chunk texts to read. Returns validated goal rows tagged with
        department/quarter/year. Never raises — returns [] on any failure."""
        from src.ingestion.schemas.goals import GoalsExtraction
        if not texts:
            return []
        try:
            body = "\n\n---\n\n".join(texts)[:14000]
            schema_json = json.dumps(GoalsExtraction.model_json_schema())
            prompt = (
                f"You extract DEPARTMENT GOALS from a City of Harrisburg quarterly report "
                f"(department: {department or 'unknown'}, period: {quarter or ''} {year or ''}).\n"
                "The report has an 'Annual Goals' / '20XX Goals' section; each goal is a short "
                "titled item with a narrative description, sometimes a quantified target and/or a "
                "progress note.\n"
                f"Extract goals matching THIS JSON schema:\n{schema_json}\n\n"
                "Rules: one row per distinct goal; 'goal_title' is the goal's heading/name; "
                "'description' a 1-2 sentence summary; 'target' ONLY if a quantified aim is stated "
                "(else ''); 'status' ONLY if progress is stated (else ''); include a verbatim "
                "'source_text' quote and 'confidence' (high|medium|low); omit low-confidence rows. "
                "If there is no goals section, return an empty list. Return ONLY the JSON object.\n\n"
                "Text:\n---\n" + body + "\n---"
            )
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            validated = GoalsExtraction.model_validate_json(raw)
            rows = [r for r in validated.model_dump()["goals"]
                    if r.get("confidence") in ("high", "medium")]
            for r in rows:
                r["department"] = department
                r["quarter"] = quarter
                r["year"] = year
            return rows
        except Exception as e:
            logger.warning("goal extraction failed for %s: %s", department, e)
            return []

    def extract_grants(self, texts: list[str], department: str = "") -> list[dict[str, Any]]:
        """Strictly extract EXTERNAL grant awards (not budget lines/spending) from a
        quarterly report, using a focused prompt. Returns validated grant rows tagged
        with department. Never raises — returns [] on failure."""
        from src.ingestion.schemas.grants import GrantsExtraction
        if not texts:
            return []
        try:
            body = "\n\n---\n\n".join(texts)[:16000]
            schema_json = json.dumps(GrantsExtraction.model_json_schema())
            prompt = (
                f"You extract GRANTS from a City of Harrisburg quarterly report "
                f"(department: {department or 'unknown'}).\n"
                "A GRANT is EXTERNAL funding awarded to (or applied for by) the City from a "
                "federal, state, county, or private/foundation source — identified by a grant "
                "or program NAME and a TOTAL AWARD amount.\n"
                "STRICT EXCLUSIONS — do NOT extract these as grants: budget line items, "
                "appropriations, revised-budget or YTD-expended figures, department spending, "
                "salaries, purchases, or internal fund transfers. If a dollar figure is the "
                "department's budget or spending (not an external award), SKIP it.\n"
                f"Return goals matching THIS JSON schema:\n{schema_json}\n\n"
                "Rules: one row per distinct grant; 'grant_name' required; 'amount' is the TOTAL "
                "award (plain number, no $/commas) or null; 'status' one of active|pending|closed|"
                "awarded|applied; include a verbatim 'source_text' quote and 'confidence' "
                "(high|medium|low); OMIT low-confidence rows and anything you are unsure is a real "
                "external grant. If there are no grants, return an empty list. Return ONLY the JSON.\n\n"
                "Text:\n---\n" + body + "\n---"
            )
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model, max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            rows = [r for r in GrantsExtraction.model_validate_json(raw).model_dump()["grants"]
                    if r.get("confidence") in ("high", "medium")]
            for r in rows:
                r["department"] = department
            return rows
        except Exception as e:
            logger.warning("grant extraction failed for %s: %s", department, e)
            return []

    def extract_meeting(self, texts: list[str], source_file: str = "") -> dict[str, list[dict[str, Any]]]:
        """Extract ONE meeting record + its actions from a council minutes document,
        using a focused prompt (more reliable than the generic schema path for minutes).
        Returns {"meetings": [...], "meeting_actions": [...]}; [] on failure."""
        from src.ingestion.schemas.minutes import MinutesExtraction
        if not texts:
            return {"meetings": [], "meeting_actions": []}
        try:
            body = "\n\n---\n\n".join(texts)[:24000]
            schema_json = json.dumps(MinutesExtraction.model_json_schema())
            prompt = (
                "You extract structured data from ONE City of Harrisburg City Council legislative "
                "session minutes document.\n"
                "Return an object with two keys:\n"
                "1. 'meetings' — EXACTLY ONE row: meeting_date (from the header date, format "
                "YYYY-MM-DD), session_type (e.g. 'Legislative Session'), president (presiding "
                "officer), members_present (INTEGER count from roll call), members_present_names "
                "(comma-separated), members_absent_names, call_to_order (e.g. '6:00PM'), adjourned.\n"
                "2. 'meeting_actions' — ONE row per resolution or ordinance acted on: item_type "
                "('resolution'|'ordinance'), item_number (e.g. '1-2026'), title (short subject), "
                "action (e.g. 'read into record','referred to committee','final passage','first reading'), "
                "committee (if referred).\n"
                f"Schema:\n{schema_json}\n\n"
                "Rules: ALWAYS fill meeting_date from the header. Include a verbatim 'source_text' "
                "and 'confidence' (high|medium|low) per row; omit low-confidence rows. Return ONLY JSON.\n\n"
                "Minutes text:\n---\n" + body + "\n---"
            )
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model, max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            data = MinutesExtraction.model_validate_json(raw).model_dump()
            keep = lambda rows: [r for r in rows if r.get("confidence") in ("high", "medium")]
            return {"meetings": keep(data["meetings"]), "meeting_actions": keep(data["meeting_actions"])}
        except Exception as e:
            logger.warning("meeting extraction failed for %s: %s", source_file, e)
            return {"meetings": [], "meeting_actions": []}

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
    # NOTE: "grants" intentionally excluded — grants now come from the strict
    # SQLExtractor.extract_grants path (external awards only), not this generic
    # extractor which over-counted budget/spending figures as grants.
    for key in ("expenditures", "metrics", "vacancies"):
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
