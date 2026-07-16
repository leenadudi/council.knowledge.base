"""
Answer synthesizer — takes retrieved results and produces a cited answer.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.llm.client import TrackedAnthropic

from src.config import Settings, get_settings
from src.models import Citation, QueryResponse, RetrievalResult

logger = logging.getLogger(__name__)

_SYNTHESIS_PROMPT = """You are an assistant for City of Harrisburg government employees.
Answer the question using ONLY the information provided below. Do not add information from your own knowledge.

Rules:
- Cite the source document and section for every piece of information you use.
- If sources conflict, state the conflict explicitly.
- If the information needed to answer is not in the provided context, say: "The information needed to answer this question was not found in the knowledge base."
- Never guess or estimate dollar figures — only report exact numbers from the data.
- Be concise and factual.
- When presenting tabular data, always use GFM pipe tables (| Col | Col |\n|---|---|\n| val | val |). Never use ASCII art or plain text alignment.

Question: {question}

Retrieved information:
{context}

Answer with citations (cite as [Source: filename, Section: section_name]):
"""


class Synthesizer:
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="query.synthesizer")

    def synthesize(
        self,
        question: str,
        retrieval_results: list[RetrievalResult],
        query_response: QueryResponse,
    ) -> QueryResponse:
        """
        Synthesize an answer from retrieval results and attach it to the QueryResponse.
        """
        context, citations = self._format_context(retrieval_results)

        # Check for weak retrieval — trigger fallback signal
        if not context.strip():
            query_response.answer = (
                "The information needed to answer this question was not found in the knowledge base."
            )
            query_response.citations = []
            return query_response

        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=4096,
                query_id=query_response.query_id,
                messages=[{
                    "role": "user",
                    "content": _SYNTHESIS_PROMPT.format(
                        question=question,
                        context=context,
                    ),
                }],
            )
            answer = msg.content[0].text.strip()
        except Exception as e:
            logger.error("Synthesis failed: %s", e)
            answer = f"Answer generation failed: {e}"

        query_response.answer = answer
        query_response.citations = citations
        query_response.stores_queried = [r.store for r in retrieval_results]
        return query_response

    def _format_context(
        self, results: list[RetrievalResult]
    ) -> tuple[str, list[Citation]]:
        """Format retrieval results into a context string and extract citations."""
        sections: list[str] = []
        citations: list[Citation] = []

        for result in results:
            if result.error:
                sections.append(f"[{result.store} retrieval error: {result.error}]")
                continue

            if result.store in ("vector", "vector_fallback") and result.chunks:
                for chunk in result.chunks:
                    payload = chunk.get("payload", {})
                    text = payload.get("text", "")
                    source = payload.get("source_file", "unknown")
                    section = payload.get("section") or None
                    dept = payload.get("department") or None
                    quarter = payload.get("quarter") or None
                    year = payload.get("year") or None
                    chunk_id = chunk.get("chunk_id") or None

                    sections.append(
                        f"[Source: {source}, Section: {section}]\n{text}"
                    )
                    citations.append(Citation(
                        source_file=source,
                        department=dept,
                        section=section,
                        quarter=quarter,
                        year=year,
                        chunk_id=chunk_id,
                    ))

            if result.store == "sql" and result.sql_rows:
                rows_text = _format_sql_rows(result.sql_rows)
                # Cite the source PDF(s), not the datastore — that's what users know.
                sql_sources = _distinct(r.get("source_file") for r in result.sql_rows)
                src = "; ".join(sql_sources) if sql_sources else "city records"
                sections.append(
                    f"[Source documents: {src} — cite each figure using the "
                    f"source_file shown in its row]\n{rows_text}"
                )
                for s in sql_sources:
                    _add_citation(citations, s)

            if result.store == "graph" and result.graph_data:
                graph_text = _format_graph_data(result.graph_data)
                graph_sources = _distinct(
                    v
                    for rec in result.graph_data.get("records", [])
                    if isinstance(rec, dict)
                    for v in rec.values()
                    if isinstance(v, str) and v.lower().endswith(".pdf")
                )
                label = (
                    f"[Source documents: {'; '.join(graph_sources)}]"
                    if graph_sources
                    else "[Organizational records from City of Harrisburg documents]"
                )
                sections.append(f"{label}\n{graph_text}")
                for s in graph_sources:
                    _add_citation(citations, s)

        return "\n\n---\n\n".join(sections), citations


def _distinct(values) -> list[str]:
    """Order-preserving unique of truthy string values."""
    seen: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.append(v)
    return seen


def _add_citation(citations: list[Citation], source_file: str) -> None:
    """Append a source-file citation unless one for that file already exists."""
    if not any(c.source_file == source_file for c in citations):
        citations.append(Citation(source_file=source_file))


def _format_sql_rows(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    headers = list(rows[0].keys())
    header_line = " | ".join(headers)
    separator = "-" * len(header_line)
    data_lines = [" | ".join(str(row.get(h, "")) for h in headers) for row in rows]
    return "\n".join([header_line, separator, *data_lines])


def _format_graph_data(data: dict) -> str:
    records = data.get("records", [])
    if not records:
        return "(no results)"
    lines = []
    for record in records:
        lines.append(str(record))
    return "\n".join(lines)
