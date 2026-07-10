"""
SQL extraction: schema-driven extraction of structured rows from documents.

Quarterly reports use the unified `extract_quarterly` pass (all chunks, one schema);
registry types (resolution/legislation) use `extract_for_type`; minutes use the
focused `extract_meeting`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.llm.client import TrackedAnthropic

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SQLExtractor:
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="ingestion.sql_extractor")

    def _schema_extract_batch(self, chunks, schema_cls) -> dict[str, list[dict]]:
        """One LLM call over a chunk batch against schema_cls. Returns raw dict of
        lists filtered to high/medium confidence. Never raises → {} on failure."""
        try:
            text = "\n\n---\n\n".join(c.text for c in chunks)
            schema_json = json.dumps(schema_cls.model_json_schema())
            prompt = (
                "You are a precise data extractor for City of Harrisburg quarterly reports.\n"
                "Extract EVERYTHING matching this JSON schema, wherever it appears in the text "
                "(sections are labeled differently by each department — do not rely on headings).\n"
                f"{schema_json}\n\n"
                "Rules: include a verbatim 'source_text' for every row; set 'confidence' "
                "high|medium|low and omit low-confidence rows; dollar amounts as plain numbers; "
                "dates YYYY-MM-DD or null. Return ONLY the JSON object.\n\nText:\n---\n" + text + "\n---"
            )
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model, max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            data = schema_cls.model_validate_json(raw).model_dump()
            return {k: [r for r in v if r.get("confidence") in ("high", "medium")]
                    for k, v in data.items() if v}
        except Exception as e:
            logger.warning("quarterly batch extraction failed: %s", e)
            return {}

    def extract_quarterly(self, chunks, department: str = "", quarter: str = "",
                          year: Optional[int] = None) -> dict[str, list[dict[str, Any]]]:
        """Unified schema-driven extraction for a whole quarterly report. Batches ALL
        chunks (no routes_to_sql gate, no keyword filter), merges rows across batches,
        tags each with department/quarter/year, strips extraction-only fields."""
        from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
        if not chunks:
            return {}
        merged: dict[str, list] = {}
        batch_size = self.cfg.extraction_batch_size
        for i in range(0, len(chunks), batch_size):
            part = self._schema_extract_batch(chunks[i:i + batch_size], QuarterlyReportExtraction)
            for key, rows in part.items():
                merged.setdefault(key, []).extend(rows)
        for rows in merged.values():
            for r in rows:
                r.pop("source_text", None)
                r.pop("confidence", None)
                r["department"] = department
                r["quarter"] = quarter
                r["year"] = year
        # A report section that straddles a batch boundary gets extracted in more
        # than one batch, yielding identical rows. Collapse exact duplicates (keeps
        # genuinely distinct rows — different value/count/name — untouched).
        def _dedup(rows):
            seen, out = set(), []
            for r in rows:
                key = tuple(sorted((str(k), str(v)) for k, v in r.items()))
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
            return out
        return {k: _dedup(v) for k, v in merged.items() if v}

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
                # missing-confidence rows are dropped intentionally: schemas mandate "confidence"
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
