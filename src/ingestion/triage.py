"""Ingest-side triage: detect structured data in an unclassified document and reconcile
it against the LIVE schema (map into existing tables vs. propose a new one). M1 only
proposes — it never writes structured rows or mutates schema."""
from __future__ import annotations
import json
import logging
from typing import Optional

from src.config import Settings, get_settings
from src.ingestion.schemas.triage import TriageResult

logger = logging.getLogger(__name__)

# Structured tables the triage agent may reconcile against.
_STRUCTURED_TABLES = (
    "expenditures", "metrics", "grants", "vacancies", "goals", "projects",
    "resolutions", "votes", "meetings", "meeting_actions", "legislation", "appropriations",
)


def schema_summary(store) -> str:
    """One line per table: `table(col1, col2, ...)`, from live information_schema."""
    with store.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) "
            "ORDER BY table_name, ordinal_position",
            (list(_STRUCTURED_TABLES),),
        )
        rows = cur.fetchall()
    cols: dict[str, list[str]] = {}
    for r in rows:
        cols.setdefault(r["table_name"], []).append(r["column_name"])
    return "\n".join(f"{t}({', '.join(c)})" for t, c in cols.items())


def build_triage_prompt(text: str, schema_text: str) -> str:
    schema_json = json.dumps(TriageResult.model_json_schema())
    return (
        "You are a data-architecture triage agent for a City of Harrisburg knowledge base.\n"
        "Decide whether this document contains structured, record-like data worth storing "
        "in SQL (rosters, tables, per-item records) — as opposed to purely narrative prose.\n\n"
        "If it does, identify each RECORD TYPE and reconcile it against the EXISTING schema "
        "below. For each record type choose a target:\n"
        "  - \"existing\": the SAME KIND of record already has a table — give existing_table "
        "and a column_mapping (doc field -> existing column). Only choose this when it is "
        "genuinely the same kind of record, not merely column-similar. When unsure, prefer "
        "\"new\" or a low match_confidence.\n"
        "  - \"new\": no existing table fits — propose columns (types limited to TEXT, "
        "VARCHAR(n), INTEGER, DECIMAL(15,2), DATE, BOOLEAN).\n"
        "For EVERY record type you MUST set match_confidence (0.0-1.0) — how sure you are the "
        "target choice is correct. This is REQUIRED; do not leave it at 0.\n"
        "Extract ONLY structured, record-like facts (rosters, seats, counts, dates, IDs). Do "
        "NOT create record types for narrative prose — overviews, 'General Function', "
        "descriptions, accountability text belong in full-text search, not SQL. Proposing a "
        "table for prose produces junk rows.\n"
        "Include up to 5 verbatim sample_rows per record type so a human can judge quality.\n\n"
        f"EXISTING TABLES:\n{schema_text}\n\n"
        f"Return ONLY JSON matching this schema:\n{schema_json}\n\n"
        f"Document:\n---\n{text}\n---"
    )


def run_triage(text: str, schema_text: str, llm, attempts: int = 2) -> TriageResult:
    """Run one triage pass. Returns an empty (has_structured_data=False) result rather
    than raising, so a triage failure never blocks ingestion."""
    prompt = build_triage_prompt(text, schema_text)
    for attempt in range(attempts):
        try:
            msg = llm.messages.create(
                model=_triage_model(), max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            return TriageResult.model_validate_json(raw)
        except Exception as e:
            logger.warning("triage attempt %d/%d failed: %s", attempt + 1, attempts, e)
    return TriageResult()


_CFG: Optional[Settings] = None


def _triage_model() -> str:
    global _CFG
    if _CFG is None:
        _CFG = get_settings()
    return _CFG.profiler_model
