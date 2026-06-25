"""
Main ingestion pipeline orchestrator.

For each document:
  1. Detect document type
  2. Parse (Unstructured or Vision LLM, with quality-check fallback)
  3. Chunk at section boundaries
  4. Tag metadata
  5. Classify content type
  6. Embed (OpenAI text-embedding-3-large)
  7. Route to vector store (always), SQL store, and graph store (where applicable)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import anthropic
import voyageai

from src.config import Settings, get_settings
from src.extraction.graph_extractor import GraphExtractor
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion import chunker, classifier, detector, metadata
from src.ingestion.parsers import unstructured_parser, vision_parser
from src.ingestion.parsers.unstructured_parser import ParseQualityError
from src.models import Chunk, ChunkMetadata
from src.storage.graph_store import GraphStore
from src.storage.sql_store import SQLStore
from src.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


class IngestionPipeline:
    def __init__(self, settings: Optional[Settings] = None):
        self.cfg = settings or get_settings()
        self.vector_store = VectorStore(self.cfg)
        self.sql_store = SQLStore(self.cfg)
        self.graph_store = GraphStore(self.cfg)
        self.sql_extractor = SQLExtractor(self.cfg)
        self.graph_extractor = GraphExtractor(self.cfg)
        self._voyage = voyageai.Client(api_key=self.cfg.voyage_api_key)
        self._anthropic = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key)

    def initialize_stores(self) -> None:
        """Set up collection/schema in all three stores."""
        self.vector_store.ensure_collection()
        self.graph_store.ensure_constraints()
        logger.info("All stores initialized")

    def ingest_directory(self, docs_dir: str | Path, skip_existing: bool = True) -> None:
        """Ingest all PDFs in a directory."""
        path = Path(docs_dir)
        pdfs = sorted(path.glob("*.pdf"))
        logger.info("Found %d PDF documents in %s", len(pdfs), path)

        for pdf in pdfs:
            if skip_existing and self.sql_store.is_document_ingested(pdf.name):
                logger.info("Skipping already-ingested: %s", pdf.name)
                continue
            try:
                self.ingest_document(pdf)
            except Exception as e:
                logger.error("Failed to ingest %s: %s", pdf.name, e, exc_info=True)

    def ingest_document(self, file_path: str | Path) -> list[Chunk]:
        """Full ingestion pipeline for a single document."""
        path = Path(file_path)
        start = time.time()
        logger.info("Ingesting: %s", path.name)

        # Step 1: Detect document type
        doc_kind = detector.detect(path, self.cfg)

        # Step 2: Parse
        parsed = self._parse_with_fallback(path, doc_kind)

        # Step 3: Chunk
        raw_chunks = chunker.chunk_document(parsed, self.cfg)
        logger.info("  → %d chunks from %s", len(raw_chunks), path.name)

        if not raw_chunks:
            logger.warning("No chunks produced for %s — skipping", path.name)
            return []

        # Step 4 + 5: Metadata + Classification
        element_types = [c.get("element_type", "NarrativeText") for c in raw_chunks]
        content_types = classifier.classify_batch(raw_chunks, element_types, self.cfg)

        total = len(raw_chunks)
        chunks: list[Chunk] = []
        for idx, (raw, ct) in enumerate(zip(raw_chunks, content_types)):
            meta_dict = metadata.build_chunk_metadata(
                chunk_dict=raw,
                source_file=path.name,
                chunk_index=idx,
                total_chunks=total,
                content_type=ct,
                parser_used=parsed.parser_used,
            )
            chunks.append(Chunk(
                text=raw["text"],
                metadata=ChunkMetadata(**meta_dict),
            ))

        # Step 6: Embed all chunks
        self._embed_chunks(chunks)

        # Step 7: Route to stores
        self._store_chunks(chunks, path.name)

        # Record ingestion
        file_meta = metadata.extract_file_metadata(path.name)
        self.sql_store.record_document(
            source_file=path.name,
            department=file_meta["department"],
            document_type="quarterly_report",
            quarter=file_meta["quarter"],
            year=file_meta["year"],
            parser_used=parsed.parser_used,
            total_chunks=total,
        )

        elapsed = time.time() - start
        logger.info("Ingested %s in %.1fs (%d chunks)", path.name, elapsed, total)
        return chunks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_with_fallback(self, path: Path, doc_kind: str):
        """Try Unstructured first for clean docs; fall back to Vision LLM if quality fails."""
        if doc_kind == "complex_pdf":
            return vision_parser.parse(path, self.cfg)

        if doc_kind in ("clean_text_pdf", "word_doc"):
            try:
                parsed = unstructured_parser.parse(path)
                return parsed
            except ParseQualityError as e:
                logger.warning("Unstructured quality check failed: %s — retrying with Vision LLM", e)
                return vision_parser.parse(path, self.cfg)

        raise ValueError(f"Unsupported document kind: {doc_kind}")

    def _embed_chunks(self, chunks: list[Chunk]) -> None:
        """Embed all chunks using the configured embedding model."""
        texts = [c.text for c in chunks]
        try:
            resp = self._voyage.embed(texts, model=self.cfg.embedding_model)
            for chunk, embedding in zip(chunks, resp.embeddings):
                chunk.embedding = embedding
        except Exception as e:
            logger.error("Embedding failed: %s", e)
            raise

    def _store_chunks(self, chunks: list[Chunk], source_file: str) -> None:
        """Route chunks to vector store (always), SQL, and graph (where applicable)."""
        # Vector store — all chunks
        self.vector_store.upsert_chunks(chunks)

        # SQL store — table and metrics chunks
        sql_chunks = [c for c in chunks if c.routes_to_sql()]
        if sql_chunks:
            sql_data = self.sql_extractor.extract_chunks_batched(sql_chunks)
            self._write_sql_data(sql_data, sql_chunks, source_file)

        # Graph store — org_data, project, and table chunks (tables can contain org info)
        graph_chunks = [c for c in chunks if c.routes_to_graph()]
        if graph_chunks:
            try:
                graph_data = self.graph_extractor.extract_chunks_batched(graph_chunks)
                self._write_graph_data(graph_data, source_file, chunks[0].metadata)
                chunk_ids = [str(c.chunk_id) for c in graph_chunks]
                self.graph_store.link_chunks_to_entities(chunk_ids, graph_data)
            except Exception as e:
                logger.warning("Graph store write failed (vector+SQL still complete): %s", e)

    def _write_sql_data(
        self,
        sql_data: dict,
        sql_chunks: list[Chunk],
        source_file: str,
    ) -> None:
        chunk_id = sql_chunks[0].chunk_id  # representative chunk for source reference

        if sql_data.get("expenditures"):
            self.sql_store.insert_expenditure_rows(
                sql_data["expenditures"], chunk_id, source_file
            )
        if sql_data.get("metrics"):
            self.sql_store.insert_metric_rows(
                sql_data["metrics"], chunk_id, source_file
            )
        if sql_data.get("grants"):
            self.sql_store.insert_grant_rows(
                sql_data["grants"], chunk_id, source_file
            )
        if sql_data.get("vacancies"):
            self.sql_store.insert_vacancy_rows(
                sql_data["vacancies"], chunk_id
            )

    def _write_graph_data(self, graph_data: dict, source_file: str, meta: ChunkMetadata) -> None:
        if graph_data.get("departments"):
            self.graph_store.upsert_departments(graph_data["departments"])
        if graph_data.get("people"):
            self.graph_store.upsert_people(graph_data["people"])
        if graph_data.get("projects"):
            self.graph_store.upsert_projects(graph_data["projects"])
        if graph_data.get("grants"):
            self.graph_store.upsert_grants(graph_data["grants"])
        if graph_data.get("relationships"):
            self.graph_store.upsert_relationships(graph_data["relationships"])

        # Link department to source document
        self.graph_store.upsert_document(source_file, meta.quarter, meta.year, meta.department)
        self.graph_store.link_department_to_document(meta.department, source_file)
