"""Agentic document profiling: read the first few pages, return a DocumentProfile
(type, department/owner, period, ids, confidence). Replaces filename-regex metadata."""
from __future__ import annotations

import json
import logging
from typing import Optional

from src.config import Settings, get_settings
from src.llm.client import TrackedAnthropic
from src.models import DocumentProfile, ParsedDocument
from src.ingestion.registry import all_document_types

logger = logging.getLogger(__name__)

_PROMPT = """You classify City of Harrisburg government documents. Read the excerpt and identify what it is.

Known document types (choose the single best match by name):
{type_menu}

Rules:
- "document_type" MUST be one of the known type names above, OR "unclassified" if none fit.
- If it looks like a real type not in the list, set "document_type" to "unclassified" and put your guess in "proposed_type".
- "department" is the owning city department/bureau/office, or the body that issued it.
- "period" is the time it covers: a quarter like "Q1 2026", a year "2026", or an adoption date "YYYY-MM-DD".
- "identifying_ids" holds stable identifiers found in the text (e.g. {{"resolution_number": "2026-R-12"}}).
- "confidence" is 0.0-1.0 — how sure you are of the document_type.
{hint_line}
Filename (weak hint only, may be misleading): {source_file}

Return ONLY a JSON object:
{{"document_type": "...", "department": "...", "period": "...", "title": "...",
  "identifying_ids": {{}}, "confidence": 0.0, "proposed_type": null}}

Document excerpt:
---
{excerpt}
---"""


def _excerpt(parsed: ParsedDocument, max_pages: int) -> str:
    parts = [e.text for e in parsed.elements if e.page_number <= max_pages]
    return "\n\n".join(parts)[:8000]


def profile_document(
    parsed: ParsedDocument,
    source_file: str,
    category_hint: Optional[str] = None,
    client: Optional[TrackedAnthropic] = None,
    settings: Optional[Settings] = None,
) -> DocumentProfile:
    cfg = settings or get_settings()
    llm = client or TrackedAnthropic(cfg, call_site="ingestion.profiler")

    type_menu = "\n".join(f"- {dt.name}: {dt.description}" for dt in all_document_types())
    hint_line = (
        f"- A source-system category hint is provided (treat as strong but verifiable): "
        f"\"{category_hint}\".\n"
    ) if category_hint else ""
    prompt = _PROMPT.format(
        type_menu=type_menu,
        hint_line=hint_line,
        source_file=source_file,
        excerpt=_excerpt(parsed, cfg.profiler_max_pages),
    )

    try:
        msg = llm.messages.create(
            model=cfg.profiler_model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
        return DocumentProfile(
            document_type=data.get("document_type", "unclassified") or "unclassified",
            department=data.get("department", "") or "",
            period=data.get("period", "") or "",
            title=data.get("title", "") or "",
            identifying_ids=data.get("identifying_ids", {}) or {},
            confidence=float(data.get("confidence", 0.0) or 0.0),
            proposed_type=data.get("proposed_type"),
        )
    except Exception as e:
        logger.warning("Profiler failed for %s: %s — marking unclassified", source_file, e)
        return DocumentProfile(document_type="unclassified", department="", confidence=0.0)
