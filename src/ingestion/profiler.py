"""Agentic document profiling: read the first few pages, return a DocumentProfile
(type, department/owner, period, ids, confidence). Replaces filename-regex metadata."""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from src.config import Settings, get_settings
from src.llm.client import TrackedAnthropic
from src.models import DocumentProfile, ParsedDocument
from src.ingestion.registry import all_document_types

logger = logging.getLogger(__name__)

_PROFILE_ATTEMPTS = 3      # transient-failure retries before giving up (→ unclassified)
_PROFILE_BACKOFF = 2.0     # seconds; multiplied by attempt number for linear backoff

_PROMPT = """You classify City of Harrisburg government documents. Read the excerpt and identify what it is.

Known document types (choose the single best match by name):
{type_menu}

Rules:
- "document_type" MUST be one of the known type names above, OR "unclassified" if none fit.
- If it looks like a real type not in the list, set "document_type" to "unclassified" and put your guess in "proposed_type".
- "department" is the owning city department/bureau/office named in the document's CONTENT (letterhead, title block, section header, or body — e.g. "PARKS & RECREATION", "Bureau of Fire", "City Council"). Normalize obvious variants to a clean official name. NEVER derive the department from the filename. If the content does not name a department, return "" (empty) — do not guess from the filename or the category hint.
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

    # Retry transient failures (rate limit / overload / flaky JSON). A single
    # hiccup must NOT silently downgrade a real document to "unclassified" and
    # discard all of its structured data, so we attempt a few times with backoff
    # before giving up.
    last_err: Optional[Exception] = None
    for attempt in range(_PROFILE_ATTEMPTS):
        try:
            msg = llm.messages.create(
                model=cfg.profiler_model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
            data = json.loads(raw)
            # identifying_ids values must be strings; the LLM sometimes returns a list
            # (e.g. minutes reference several resolution numbers). Coerce lists to a
            # comma-joined string and drop empties, so a rich doc never fails classification.
            raw_ids = data.get("identifying_ids", {}) or {}
            ids = {
                str(k): (", ".join(str(x) for x in v) if isinstance(v, list) else str(v))
                for k, v in raw_ids.items()
                if v not in (None, "", [], {})
            }
            return DocumentProfile(
                document_type=data.get("document_type", "unclassified") or "unclassified",
                department=data.get("department", "") or "",
                period=data.get("period", "") or "",
                title=data.get("title", "") or "",
                identifying_ids=ids,
                confidence=float(data.get("confidence", 0.0) or 0.0),
                proposed_type=data.get("proposed_type"),
            )
        except Exception as e:
            last_err = e
            if attempt < _PROFILE_ATTEMPTS - 1:
                logger.warning("Profiler attempt %d/%d failed for %s: %s — retrying",
                               attempt + 1, _PROFILE_ATTEMPTS, source_file, e)
                time.sleep(_PROFILE_BACKOFF * (attempt + 1))

    logger.error("Profiler failed for %s after %d attempts: %s — marking unclassified",
                 source_file, _PROFILE_ATTEMPTS, last_err)
    return DocumentProfile(document_type="unclassified", department="", confidence=0.0)
