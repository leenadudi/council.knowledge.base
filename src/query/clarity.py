"""
Query clarity assessment.

Computes whether a query's retrieval is "weak" — i.e. likely to produce a
vague, empty-headings answer — using three signals from the retrieved chunks:

  1. top_score      — best cosine similarity. Low → nothing semantically close.
  2. mean_score     — average similarity across retrieved chunks. Low → scattered/thin.
  3. header_ratio   — fraction of chunks that are headers/titles rather than body
                      text. High → retrieval grabbed section titles, not substance.

During soft launch this is LOGGED ONLY (clarity_gate_enabled = False). The
assessment is written to query_logs.clarity_assessment so thresholds can be
calibrated against real traffic before the gate is turned on.
"""

from __future__ import annotations

from typing import Any, Optional

from src.config import Settings, get_settings
from src.models import RetrievalResult

# Content types that are "thin" — a header/title with little body text.
_THIN_CONTENT_TYPES = {"header"}


def assess_retrieval(
    results: list[RetrievalResult],
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """
    Score the retrieval quality for a query and decide whether it would be
    flagged as weak. Returns a JSON-serializable assessment dict.
    """
    cfg = settings or get_settings()

    # Gather all vector chunks (primary + fallback + graph mention chunks).
    chunks: list[dict] = []
    for r in results:
        if r.chunks:
            chunks.extend(r.chunks)

    # Structured hits (SQL/graph) count as strong evidence on their own.
    has_sql = any(r.store == "sql" and r.sql_rows for r in results)
    has_graph = any(
        r.store == "graph" and r.graph_data and r.graph_data.get("records")
        for r in results
    )

    scores = [float(c.get("score", 0.0)) for c in chunks]
    top_score = max(scores) if scores else 0.0
    mean_score = (sum(scores) / len(scores)) if scores else 0.0

    num_chunks = len(chunks)
    distinct_docs = len({
        c.get("payload", {}).get("source_file") for c in chunks
        if c.get("payload", {}).get("source_file")
    })

    header_chunks = sum(
        1 for c in chunks
        if c.get("payload", {}).get("content_type") in _THIN_CONTENT_TYPES
    )
    header_ratio = (header_chunks / num_chunks) if num_chunks else 0.0

    # --- Decide ---
    # Conservative: only the strongest signal (nothing semantically close, or
    # nothing retrieved at all) actually flags a query. The softer signals
    # (low mean score, mostly-headers) are recorded for calibration but do NOT
    # trigger a flag on their own — they're too noisy and would nag users who
    # asked perfectly answerable questions.
    reasons: list[str] = []
    soft_signals: list[str] = []

    if mean_score < cfg.clarity_min_mean_score:
        soft_signals.append("low_mean_score")
    if header_ratio > cfg.clarity_max_header_ratio:
        soft_signals.append("mostly_headers")

    # Structured data (SQL/graph) answers the question regardless of vector quality.
    if has_sql or has_graph:
        would_flag = False
    elif num_chunks == 0:
        would_flag = True
        reasons.append("no_chunks")
    elif top_score < cfg.clarity_min_top_score:
        would_flag = True
        reasons.append("low_top_score")
    else:
        would_flag = False

    return {
        "would_flag": would_flag,
        "reasons": reasons,
        "soft_signals": soft_signals,
        "top_score": round(top_score, 4),
        "mean_score": round(mean_score, 4),
        "num_chunks": num_chunks,
        "distinct_docs": distinct_docs,
        "header_ratio": round(header_ratio, 4),
        "has_sql": has_sql,
        "has_graph": has_graph,
        "thresholds": {
            "min_top_score": cfg.clarity_min_top_score,
            "min_mean_score": cfg.clarity_min_mean_score,
            "max_header_ratio": cfg.clarity_max_header_ratio,
        },
    }
