"""
Query pipeline orchestrator — ties classifier, retriever, and synthesizer together.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from src.config import Settings, get_settings
from src.models import QueryResponse, RetrievalResult
from src.query.classifier import QueryClassifier
from src.query.clarity import assess_retrieval
from src.query.retriever import Retriever
from src.query.synthesizer import Synthesizer
from src.storage.graph_store import GraphStore
from src.storage.sql_store import SQLStore
from src.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

# Weak retrieval: fewer than this many useful chunks → trigger fallback
_MIN_USEFUL_CHUNKS = 1

# Cap on prior turns passed into the classifier (bounds prompt tokens).
_MAX_HISTORY_TURNS = 2


class QueryPipeline:
    def __init__(
        self,
        vector_store: Optional[VectorStore] = None,
        sql_store: Optional[SQLStore] = None,
        graph_store: Optional[GraphStore] = None,
        settings: Optional[Settings] = None,
    ):
        self.cfg = settings or get_settings()
        _vs = vector_store or VectorStore(self.cfg)
        _ss = sql_store or SQLStore(self.cfg)
        _gs = graph_store or GraphStore(self.cfg)

        self.classifier = QueryClassifier(self.cfg)
        self.retriever = Retriever(_vs, _ss, _gs, self.cfg)
        self.synthesizer = Synthesizer(self.cfg)
        self.sql_store = _ss

    def ask(
        self,
        question: str,
        history: Optional[list[dict]] = None,
        log_query: bool = True,
    ) -> QueryResponse:
        """
        Full query pipeline: classify → retrieve → synthesize → log.
        Total synchronous LLM calls: 2 (classification + synthesis).

        `history` is an optional list of prior turns ({"question", "answer"});
        only the last _MAX_HISTORY_TURNS are used, and only the classifier sees
        them — retrieval and synthesis stay stateless.
        """
        start_ms = time.time()
        query_id = str(uuid.uuid4())

        capped_history = history[-_MAX_HISTORY_TURNS:] if history else None

        response = QueryResponse(
            query_id=query_id,
            question=question,
            answer="",
            timestamp=datetime.utcnow().isoformat(),
        )

        # Step 1: Query classification (1 LLM call)
        plan = self.classifier.classify(question, history=capped_history, query_id=query_id)
        logger.info(
            "Query plan — sources: %s, execution: %s",
            plan.sources, plan.execution,
        )

        # A follow-up may have been rewritten into a standalone question; use it
        # for retrieval fallback and synthesis. History itself never flows past here.
        effective_question = plan.resolved_question or question

        # Step 2: Retrieval (0 LLM calls)
        results = self.retriever.retrieve(plan)

        # Check for weak retrieval and fall back if needed
        if _is_weak_retrieval(results):
            results.extend(self.retriever.fallback_retrieve(effective_question, results))

        # Clarity assessment (soft launch: logged only, gate not enforced).
        clarity = assess_retrieval(results, self.cfg)
        if clarity["would_flag"]:
            logger.info(
                "CLARITY would_flag — reasons=%s top=%.3f mean=%.3f header_ratio=%.2f q=%r",
                clarity["reasons"], clarity["top_score"], clarity["mean_score"],
                clarity["header_ratio"], effective_question[:80],
            )

        # Step 3: Synthesis (1 LLM call)
        response = self.synthesizer.synthesize(effective_question, results, response)

        elapsed_ms = int((time.time() - start_ms) * 1000)
        response.total_time_ms = elapsed_ms

        # Log the query asynchronously (best-effort)
        if log_query:
            try:
                self._log_query(response, plan, results, clarity)
            except Exception as e:
                logger.warning("Query logging failed: %s", e)

        return response

    def _log_query(
        self,
        response: QueryResponse,
        plan,
        results: list[RetrievalResult],
        clarity: Optional[dict] = None,
    ) -> None:
        sql_result = next((r for r in results if r.store == "sql"), None)
        vector_candidates = [r for r in results if r.store in ("vector", "vector_fallback")]
        vector_result = max(vector_candidates, key=lambda r: len(r.chunks), default=None)
        graph_result = next((r for r in results if r.store == "graph"), None)

        self.sql_store.log_query({
            "query_id": response.query_id,
            "question": response.question,
            "timestamp": response.timestamp,
            "classification": plan.model_dump(),
            "sql_query": plan.sql_query,
            "chunks_retrieved": [c for c in (vector_result.chunks if vector_result else [])],
            "stores_queried": response.stores_queried,
            "sql_results": sql_result.sql_rows if sql_result else [],
            "vector_results": [c.get("payload", {}) for c in (vector_result.chunks if vector_result else [])],
            "graph_results": graph_result.graph_data if graph_result else {},
            "final_answer": response.answer,
            "citations": [c.model_dump() for c in response.citations],
            "total_time_ms": response.total_time_ms,
            "clarity_assessment": clarity,
        })


def _is_weak_retrieval(results: list[RetrievalResult]) -> bool:
    """Return True if retrieval produced very little useful content."""
    for result in results:
        if result.store == "vector" and len(result.chunks) >= _MIN_USEFUL_CHUNKS:
            return False
        if result.store == "sql" and result.sql_rows:
            return False
        if result.store == "graph" and result.graph_data.get("records"):
            return False
    return True
