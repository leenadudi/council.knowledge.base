"""
SQL extraction: schema-driven extraction of structured rows from documents.

Quarterly reports use the unified `extract_quarterly` pass (all chunks, one schema);
registry types (resolution/legislation) use `extract_for_type`; minutes use the
focused `extract_meeting`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, get_args

from src.llm.client import TrackedAnthropic

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SQLExtractor:
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="ingestion.sql_extractor")

    # -- shared quarterly extraction primitives (used by the sync path here AND by
    #    the async Batch API backfill in scripts/reextract_quarterly_batch.py) ----

    @staticmethod
    def quarterly_prompt(texts: list[str], schema_cls, doc_label: str = "quarterly reports") -> str:
        """Build the extraction prompt for one chunk-batch. Format-agnostic: the model
        finds targets wherever they appear rather than relying on section headings.
        doc_label names the document kind (default 'quarterly reports'; data-driven types
        pass their own name)."""
        body = "\n\n---\n\n".join(texts)
        schema_json = json.dumps(schema_cls.model_json_schema())
        return (
            f"You are a precise data extractor for City of Harrisburg {doc_label}.\n"
            "Extract EVERYTHING matching this JSON schema, wherever it appears in the text "
            "(sections are labeled differently by each department — do not rely on headings).\n"
            f"{schema_json}\n\n"
            "Rules: include a verbatim 'source_text' for every row; set 'confidence' "
            "high|medium|low and omit low-confidence rows; dollar amounts as plain numbers; "
            "dates YYYY-MM-DD or null. Return ONLY the JSON object.\n\nText:\n---\n" + body + "\n---"
        )

    @staticmethod
    def parse_quarterly_response(raw: str, schema_cls, raise_on_error: bool = False) -> dict[str, list[dict]]:
        """Validate one LLM response against schema_cls; keep high/medium confidence
        rows (confidence compared case-insensitively). Rows are validated INDIVIDUALLY
        so a single malformed row (e.g. a non-numeric metric_value) is skipped rather
        than discarding every row in the chunk-batch. Raises only on unparseable JSON;
        by default that returns {} (the async batch path can't re-roll a completed
        result), while raise_on_error=True lets the sync path retry."""
        try:
            raw = raw.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("expected a JSON object")
        except Exception as e:
            if raise_on_error:
                raise
            logger.warning("quarterly response parse failed: %s", e)
            return {}
        out: dict[str, list[dict]] = {}
        for field, info in schema_cls.model_fields.items():
            rows = obj.get(field)
            if not isinstance(rows, list):
                continue
            args = get_args(info.annotation)
            if not args:
                continue
            row_model = args[0]
            kept = []
            for rr in rows:
                try:
                    v = row_model.model_validate(rr).model_dump()
                except Exception:
                    continue  # drop just this row, keep the rest of the batch
                if str(v.get("confidence", "")).strip().lower() in ("high", "medium"):
                    kept.append(v)
            if kept:
                out[field] = kept
        return out

    @staticmethod
    def merge_quarterly_parts(parts: list[dict], department: str, quarter: str,
                              year: Optional[int]) -> dict[str, list[dict[str, Any]]]:
        """Merge per-batch extraction results into one report's rows: concatenate,
        strip extraction-only fields, tag with period, then collapse exact duplicates.
        A section straddling a batch boundary gets extracted in >1 batch → identical
        rows; dedup drops those while leaving genuinely distinct rows untouched."""
        merged: dict[str, list] = {}
        for part in parts:
            for key, rows in part.items():
                merged.setdefault(key, []).extend(rows)
        # Deterministic cleanup: drop expenditure subtotal/total rows (e.g.
        # "TOTAL VEH/EQUIP PARTS AND SUPPLIES") — they duplicate the detail lines
        # they sum, so keeping them double-counts any spending aggregate.
        if merged.get("expenditures"):
            merged["expenditures"] = [
                r for r in merged["expenditures"]
                if not str(r.get("line_item", "")).strip().upper().startswith("TOTAL")
            ]
        for rows in merged.values():
            for r in rows:
                r.pop("source_text", None)
                r.pop("confidence", None)
                r["department"] = department
                r["quarter"] = quarter
                r["year"] = year

        def _dedup(rows):
            seen, out = set(), []
            for r in rows:
                key = tuple(sorted((str(k), str(v)) for k, v in r.items()))
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
            return out

        # Projects: the same initiative appears across several chunks with slightly
        # different description/status, so exact-row dedup misses it (observed:
        # "Operation Municipal Migration Project" ×6). Collapse by normalized name,
        # keeping the most-detailed row (longest description).
        def _dedup_projects(rows):
            best: dict[str, dict] = {}
            for r in rows:
                k = str(r.get("project_name", "")).strip().lower()
                if k not in best or len(str(r.get("description") or "")) > len(str(best[k].get("description") or "")):
                    best[k] = r
            return list(best.values())

        out = {}
        for k, v in merged.items():
            if not v:
                continue
            out[k] = _dedup_projects(_dedup(v)) if k == "projects" else _dedup(v)
        return out

    def _schema_extract_batch(self, chunks, schema_cls, attempts: int = 3,
                              doc_label: str = "quarterly reports") -> dict[str, list[dict]]:
        """One synchronous LLM call over a chunk batch, with retry. The model
        occasionally emits invalid JSON (a stray unescaped char) non-deterministically —
        a re-roll almost always fixes it, so retry on parse failure. Also uses a 16000
        max_tokens ceiling so dense batches don't truncate. Never raises → {}."""
        prompt = self.quarterly_prompt([c.text for c in chunks], schema_cls, doc_label=doc_label)
        for attempt in range(attempts):
            try:
                msg = self.client.messages.create(
                    model=self.cfg.synthesis_model, max_tokens=16000,
                    messages=[{"role": "user", "content": prompt}],
                )
                return self.parse_quarterly_response(msg.content[0].text, schema_cls, raise_on_error=True)
            except Exception as e:
                logger.warning("quarterly batch extract attempt %d/%d failed: %s", attempt + 1, attempts, e)
        return {}

    def extract_quarterly(self, chunks, department: str = "", quarter: str = "",
                          year: Optional[int] = None) -> dict[str, list[dict[str, Any]]]:
        """Unified schema-driven extraction for a whole quarterly report (synchronous).
        Batches ALL chunks (no routes_to_sql gate, no keyword filter), then merges +
        dedups + tags. The async Batch API path reuses the same primitives."""
        from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
        if not chunks:
            return {}
        batch_size = self.cfg.extraction_batch_size
        parts = [self._schema_extract_batch(chunks[i:i + batch_size], QuarterlyReportExtraction)
                 for i in range(0, len(chunks), batch_size)]
        return self.merge_quarterly_parts(parts, department, quarter, year)

    @staticmethod
    def merge_type_parts(parts: list[dict], sql_targets: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Merge batched extraction parts for a data-driven type: keep only the type's
        sql_targets, strip extraction-only fields, and drop exact-duplicate rows (a section
        straddling a batch boundary is extracted twice). No quarterly-specific cleanup."""
        merged: dict[str, list] = {}
        for part in parts:
            for key, rows in (part or {}).items():
                if key in sql_targets and rows:
                    merged.setdefault(key, []).extend(rows)
        out: dict[str, list] = {}
        for key, rows in merged.items():
            seen, cleaned = set(), []
            for r in rows:
                r = {k: v for k, v in r.items() if k not in ("source_text", "confidence")}
                sig = tuple(sorted((str(k), str(v)) for k, v in r.items()))
                if sig in seen:
                    continue
                seen.add(sig)
                cleaned.append(r)
            if cleaned:
                out[key] = cleaned
        return out

    def extract_type_batched(self, chunks, doc_type, char_budget: Optional[int] = None
                             ) -> dict[str, list[dict[str, Any]]]:
        """Batched schema-driven extraction for a DATA-DRIVEN type (onboarded via triage).
        Batches by a CHARACTER BUDGET rather than a fixed chunk count: the section-aware
        chunker over-splits roster-style docs (each board fragments into Overview/Seats/
        Members sub-chunks, and a seat's chunk may not even contain its board name), so
        tiny fixed batches fragment a record's context across boundaries — losing the
        board↔seat association and duplicating per-board rows. Packing chunks up to a
        char budget keeps each section whole in one batch while still capping output so a
        huge document can't truncate a single call. Then merge/dedup over sql_targets."""
        if not chunks or doc_type is None or doc_type.extraction_schema is None:
            return {}
        budget = char_budget or getattr(self.cfg, "data_driven_char_budget", 24000)
        batches, cur, cur_len = [], [], 0
        for c in chunks:
            t = len(c.text or "")
            if cur and cur_len + t > budget:
                batches.append(cur); cur, cur_len = [], 0
            cur.append(c); cur_len += t
        if cur:
            batches.append(cur)
        parts = [self._schema_extract_batch(b, doc_type.extraction_schema, doc_label=doc_type.name)
                 for b in batches]
        return self.merge_type_parts(parts, doc_type.sql_targets)

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
