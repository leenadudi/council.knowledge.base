"""pgvector-backed vector store — uses the existing Supabase PostgreSQL database."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from src.config import Settings, get_settings
from src.models import Chunk

logger = logging.getLogger(__name__)


class VectorStore:
    """PostgreSQL/pgvector store for dense similarity search."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=self.settings.database_url,
        )

    def ensure_collection(self) -> None:
        """Enable pgvector and create the document_chunks table if needed."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS document_chunks (
                        chunk_id      UUID PRIMARY KEY,
                        embedding     vector({self.settings.embedding_dimensions}),
                        text          TEXT,
                        department    VARCHAR(100),
                        quarter       VARCHAR(5),
                        year          INTEGER,
                        content_type  VARCHAR(50),
                        document_type VARCHAR(50),
                        source_file   VARCHAR(255),
                        section       VARCHAR(255),
                        payload       JSONB
                    );
                """)
                # HNSW index — works well on small-to-medium datasets, no min-row requirement
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_embedding
                    ON document_chunks
                    USING hnsw (embedding vector_cosine_ops);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_source_file
                    ON document_chunks(source_file);
                """)
            conn.commit()
            logger.info("pgvector table ready (dim=%d)", self.settings.embedding_dimensions)
        finally:
            self._pool.putconn(conn)

    def upsert_chunks(self, chunks: list[Chunk]) -> None:
        """Upsert a batch of chunks. Each chunk must have an embedding."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                for chunk in chunks:
                    if chunk.embedding is None:
                        logger.warning("Skipping chunk %s — no embedding", chunk.chunk_id)
                        continue

                    meta = chunk.metadata.to_dict()
                    payload = {"text": chunk.text, **meta}
                    vec_str = "[" + ",".join(map(str, chunk.embedding)) + "]"

                    cur.execute(
                        """
                        INSERT INTO document_chunks
                            (chunk_id, embedding, text, department, quarter, year,
                             content_type, document_type, source_file, section, payload)
                        VALUES (%s, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            embedding     = EXCLUDED.embedding,
                            text          = EXCLUDED.text,
                            department    = EXCLUDED.department,
                            quarter       = EXCLUDED.quarter,
                            year          = EXCLUDED.year,
                            content_type  = EXCLUDED.content_type,
                            document_type = EXCLUDED.document_type,
                            source_file   = EXCLUDED.source_file,
                            section       = EXCLUDED.section,
                            payload       = EXCLUDED.payload;
                        """,
                        (
                            str(chunk.chunk_id),
                            vec_str,
                            chunk.text,
                            meta.get("department"),
                            meta.get("quarter"),
                            meta.get("year"),
                            meta.get("content_type"),
                            meta.get("document_type"),
                            meta.get("source_file"),
                            meta.get("section"),
                            json.dumps(payload),
                        ),
                    )
            conn.commit()
            logger.debug("Upserted %d chunks", len(chunks))
        finally:
            self._pool.putconn(conn)

    def hybrid_search(
        self,
        query_vector: list[float],
        query_text: str,
        top_k: int = 5,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Cosine similarity search with optional metadata filters."""
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                vec_str = "[" + ",".join(map(str, query_vector)) + "]"

                where_clauses: list[str] = []
                params: list[Any] = [vec_str]  # score calculation

                for key, value in (filters or {}).items():
                    where_clauses.append(f"{key} = %s")
                    params.append(value)

                where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
                params += [vec_str, top_k]  # ORDER BY vector, LIMIT

                cur.execute(
                    f"""
                    SELECT chunk_id,
                           payload,
                           1 - (embedding <=> %s::vector) AS score
                    FROM   document_chunks
                    {where_sql}
                    ORDER  BY embedding <=> %s::vector
                    LIMIT  %s;
                    """,
                    params,
                )
                rows = cur.fetchall()

            return [
                {
                    "chunk_id": str(row["chunk_id"]),
                    "score":   float(row["score"]),
                    "payload": dict(row["payload"]) if row["payload"] else {},
                }
                for row in rows
            ]
        finally:
            self._pool.putconn(conn)

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """Fetch specific chunks by their IDs — used by graph-driven retrieval."""
        if not chunk_ids:
            return []
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT chunk_id, payload FROM document_chunks WHERE chunk_id = ANY(%s::uuid[])",
                    (chunk_ids,),
                )
                rows = cur.fetchall()
            return [
                {
                    "chunk_id": str(row["chunk_id"]),
                    "score":    1.0,  # exact match via entity link
                    "payload":  dict(row["payload"]) if row["payload"] else {},
                }
                for row in rows
            ]
        finally:
            self._pool.putconn(conn)

    def delete_by_source_file(self, source_file: str) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM document_chunks WHERE source_file = %s",
                    (source_file,),
                )
            conn.commit()
        finally:
            self._pool.putconn(conn)
