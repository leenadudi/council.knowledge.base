"""
Multi-store retriever.
Executes the QueryPlan against vector, SQL, and graph stores.
No LLM calls in this step — pure database/search operations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import voyageai

from src.config import Settings, get_settings
from src.models import QueryPlan, RetrievalResult
from src.storage.graph_store import GraphStore
from src.storage.sql_store import SQLStore
from src.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


class Retriever:
    def __init__(
        self,
        vector_store: VectorStore,
        sql_store: SQLStore,
        graph_store: GraphStore,
        settings: Optional[Settings] = None,
    ):
        self.vector_store = vector_store
        self.sql_store = sql_store
        self.graph_store = graph_store
        self.cfg = settings or get_settings()
        self._voyage = voyageai.Client(api_key=self.cfg.voyage_api_key)

    def retrieve(self, plan: QueryPlan) -> list[RetrievalResult]:
        """
        Execute the retrieval plan.
        Returns a list of RetrievalResult, one per store queried.
        """
        if plan.execution == "sequential" and plan.sequential_order:
            return self._retrieve_sequential(plan)
        return self._retrieve_parallel(plan)

    def _retrieve_parallel(self, plan: QueryPlan) -> list[RetrievalResult]:
        """Query all required stores simultaneously."""
        results = []
        if "vector" in plan.sources and plan.vector_query:
            results.append(self._query_vector(plan))
        if "sql" in plan.sources and plan.sql_query:
            results.append(self._query_sql(plan))
        if "graph" in plan.sources and plan.graph_query:
            results.append(self._query_graph(plan))
        return results

    def _retrieve_sequential(self, plan: QueryPlan) -> list[RetrievalResult]:
        """
        Query stores in sequence. The result of the first store can be used
        to enrich the second query (currently passed as-is; future: inject context).
        """
        results = []
        order = plan.sequential_order or plan.sources

        for store in order:
            if store == "vector" and plan.vector_query:
                results.append(self._query_vector(plan))
            elif store == "sql" and plan.sql_query:
                results.append(self._query_sql(plan))
            elif store == "graph" and plan.graph_query:
                results.append(self._query_graph(plan))

        return results

    # ------------------------------------------------------------------
    # Per-store retrieval
    # ------------------------------------------------------------------

    def _query_vector(self, plan: QueryPlan) -> RetrievalResult:
        try:
            embedding = self._embed(plan.vector_query)
            chunks = self.vector_store.hybrid_search(
                query_vector=embedding,
                query_text=plan.vector_query,
                top_k=5,
                filters=plan.metadata_filters or None,
            )
            return RetrievalResult(store="vector", chunks=chunks)
        except Exception as e:
            logger.error("Vector retrieval failed: %s", e)
            return RetrievalResult(store="vector", error=str(e))

    def _query_sql(self, plan: QueryPlan) -> RetrievalResult:
        try:
            rows = self.sql_store.execute_query(plan.sql_query)
            return RetrievalResult(store="sql", sql_rows=rows)
        except Exception as e:
            logger.error("SQL retrieval failed: %s\nQuery: %s", e, plan.sql_query)
            return RetrievalResult(store="sql", error=str(e))

    def _query_graph(self, plan: QueryPlan) -> RetrievalResult:
        try:
            data = self.graph_store.execute_cypher(plan.graph_query)

            # Collect entity names from graph results, then fetch chunks that MENTION them.
            # This surfaces the raw source text alongside the structured entity data.
            entity_names = list({
                v for record in data for v in record.values()
                if isinstance(v, str) and len(v) > 2
            })
            mention_chunks: list = []
            if entity_names:
                chunk_ids = self.graph_store.get_chunk_ids_for_entities(entity_names[:10])
                if chunk_ids:
                    mention_chunks = self.vector_store.get_chunks_by_ids(chunk_ids[:5])

            return RetrievalResult(
                store="graph",
                graph_data={"records": data},
                chunks=mention_chunks,
            )
        except Exception as e:
            logger.error("Graph retrieval failed: %s\nQuery: %s", e, plan.graph_query)
            return RetrievalResult(store="graph", error=str(e))

    def _embed(self, text: str) -> list[float]:
        resp = self._voyage.embed([text], model=self.cfg.embedding_model)
        return resp.embeddings[0]

    # ------------------------------------------------------------------
    # Fallback retrieval
    # ------------------------------------------------------------------

    def fallback_retrieve(self, question: str, original_results: list[RetrievalResult]) -> list[RetrievalResult]:
        """
        If initial retrieval returned weak results, retry with a broader vector search
        and no metadata filters.
        """
        logger.info("Triggering fallback retrieval for: %s", question[:80])
        embedding = self._embed(question)
        chunks = self.vector_store.hybrid_search(
            query_vector=embedding,
            query_text=question,
            top_k=8,
            filters=None,
        )
        return [RetrievalResult(store="vector_fallback", chunks=chunks)]
