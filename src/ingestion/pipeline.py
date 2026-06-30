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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import anthropic
import voyageai

from src.config import Settings, get_settings
from src.extraction.graph_extractor import GraphExtractor
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion import chunker, classifier, detector, metadata
from src.ingestion.names import normalize_person_name
from src.ingestion.parsers import unstructured_parser, vision_parser
from src.ingestion.parsers.unstructured_parser import ParseQualityError
from src.ingestion.profiler import profile_document
from src.ingestion.registry import get_document_type
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

    def initialize_stores(self) -> None:
        """Set up collection/schema in all three stores."""
        self.vector_store.ensure_collection()
        self.graph_store.ensure_constraints()
        logger.info("All stores initialized")

    def ingest_directory(
        self,
        docs_dir: str | Path,
        skip_existing: bool = True,
        max_workers: int | None = None,
    ) -> None:
        """Ingest all PDFs in a directory using a bounded worker pool.

        Pre-filters already-ingested docs when skip_existing=True, then submits
        each remaining PDF to a ThreadPoolExecutor. Per-document failures are
        caught and logged so one bad document never aborts the batch.
        """
        path = Path(docs_dir)
        pdfs = sorted(path.glob("*.pdf"))
        todo = [
            p for p in pdfs
            if not (skip_existing and self.sql_store.is_document_ingested(p.name))
        ]
        workers = min(max_workers or self.cfg.ingest_workers, max(1, len(todo)))
        logger.info(
            "Ingesting %d/%d documents with %d workers",
            len(todo), len(pdfs), workers,
        )
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self._ingest_one_safe, p): p for p in todo}
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logger.error("Failed to ingest %s: %s", p.name, e, exc_info=True)

    def _ingest_one_safe(self, path: Path, attempts: int = 3):
        """Call ingest_document with bounded exponential backoff on rate-limit errors.

        On a 429/rate-limit error, sleeps 2**i seconds and retries.
        Non-rate-limit errors are re-raised immediately.
        Raises RuntimeError after exhausting all attempts.
        """
        for i in range(attempts):
            try:
                return self.ingest_document(path)
            except Exception as e:
                msg = str(e).lower()
                if isinstance(e, anthropic.RateLimitError) or "429" in msg or "rate limit" in msg:
                    wait = 2 ** i
                    logger.warning(
                        "Rate-limit hit for %s (attempt %d/%d) — retrying in %ds",
                        path.name, i + 1, attempts, wait,
                    )
                    if i < attempts - 1:
                        time.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Giving up on {path.name} after {attempts} attempts")

    def ingest_document(self, file_path: str | Path, category_hint: Optional[str] = None) -> list[Chunk]:
        """Full ingestion pipeline for a single document.

        Profiles the document, looks up its declared type, and routes:
          - low-confidence / unclassified / unknown type -> quarantine (vector-only,
            needs_review=True), while still recording the profiled document_type.
          - known type -> chunk with the type's hints, classify with its vocab,
            extract against its schema, and route to that type's SQL/graph targets.
        """
        path = Path(file_path)
        start = time.time()
        logger.info("Ingesting: %s", path.name)

        # Idempotent cleanup: clear prior data when re-ingesting an existing document.
        # Gated on is_document_ingested so first-time ingestion is a no-op.
        # graph clear is best-effort (graph writes are already wrapped in try/except).
        if self.sql_store.is_document_ingested(path.name):
            logger.info("Re-ingest: clearing prior data for %s", path.name)
            self.vector_store.delete_by_source_file(path.name)
            self.sql_store.delete_structured_rows(path.name)
            try:
                self.graph_store.clear_document_data(path.name)
            except Exception as e:
                logger.warning("graph clear failed for %s: %s", path.name, e)

        # Step 1: Detect document kind (parser selection — clean text vs. complex PDF)
        doc_kind = detector.detect(path, self.cfg)

        # Step 2: Parse
        parsed = self._parse_with_fallback(path, doc_kind)

        # Step 3: Profile (agentic) — replaces filename-regex metadata
        profile = profile_document(parsed, path.name, category_hint, settings=self.cfg)
        quarantined = self._is_quarantined(profile)
        doc_type = get_document_type(profile.document_type) if not quarantined else None
        logger.info(
            "  → profiled %s as %s (confidence %.2f)%s",
            path.name, profile.document_type, profile.confidence,
            " — QUARANTINED (needs review)" if quarantined else "",
        )

        # Step 4: Chunk (with the type's chunking hints when known)
        raw_chunks = chunker.chunk_document(
            parsed, self.cfg, hints=doc_type.chunking if doc_type else None
        )
        logger.info("  → %d chunks from %s", len(raw_chunks), path.name)

        if not raw_chunks:
            logger.warning("No chunks produced for %s — skipping", path.name)
            return []

        # Step 5 + 6: Classification (constrained to the type's vocab when known) + metadata
        element_types = [c.get("element_type", "NarrativeText") for c in raw_chunks]
        content_types = classifier.classify_batch(
            raw_chunks, element_types, self.cfg,
            vocab=doc_type.content_vocab if doc_type else None,
        )

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
                profile=profile,
                needs_review=quarantined,
            )
            chunks.append(Chunk(
                text=raw["text"],
                metadata=ChunkMetadata(**meta_dict),
            ))

        # Step 7: Embed all chunks
        self._embed_chunks(chunks)

        # Step 8: Route to stores (quarantined docs go to the vector store only)
        self._store_chunks(chunks, path.name, doc_type, quarantined, profile)

        # Record ingestion — period (quarter/year) and department come from the profile
        quarter, year = metadata._split_period(profile.period)
        self.sql_store.record_document(
            source_file=path.name,
            department=profile.department or "Unknown Department",
            document_type=profile.document_type,
            quarter=quarter,
            year=year,
            parser_used=parsed.parser_used,
            total_chunks=total,
        )

        elapsed = time.time() - start
        logger.info("Ingested %s in %.1fs (%d chunks)", path.name, elapsed, total)
        return chunks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_quarantined(self, profile) -> bool:
        """Quarantine (vector-only, needs_review) when we cannot confidently route.

        True if the profiled type is "unclassified", is not a declared/registered
        type, or the confidence is below the configured threshold.
        """
        if profile.document_type == "unclassified":
            return True
        if get_document_type(profile.document_type) is None:
            return True
        return profile.confidence < self.cfg.profile_confidence_threshold

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

    def _store_chunks(self, chunks: list[Chunk], source_file: str, doc_type, quarantined: bool, profile=None) -> None:
        """Route chunks to the vector store (always) and — for confidently-typed
        documents — to the SQL and graph stores per the document type's targets.

        Quarantined documents are written to the vector store ONLY (no SQL/graph),
        so unreviewed/low-confidence content never reaches the structured stores.
        """
        # Vector store — all chunks (including quarantined, for retrieval + review)
        self.vector_store.upsert_chunks(chunks)

        if quarantined or doc_type is None:
            return

        # quarterly_report keeps its EXISTING extraction/routing path unchanged
        # (regression-safe): generic batched SQL extraction + the legacy graph path.
        if doc_type.name == "quarterly_report":
            sql_chunks = [c for c in chunks if c.routes_to_sql()]
            if sql_chunks:
                sql_data = self.sql_extractor.extract_chunks_batched(sql_chunks)
                self._write_sql_data(sql_data, sql_chunks, source_file)

            graph_chunks = [c for c in chunks if c.routes_to_graph()]
            if graph_chunks:
                try:
                    graph_data = self.graph_extractor.extract_chunks_batched(graph_chunks)
                    self._write_graph_data(graph_data, source_file, chunks[0].metadata)
                    chunk_ids = [str(c.chunk_id) for c in graph_chunks]
                    self.graph_store.link_chunks_to_entities(chunk_ids, graph_data)
                except Exception as e:
                    logger.warning("Graph store write failed (vector+SQL still complete): %s", e)
            return

        # Other known types (e.g. resolution): schema-driven extraction routed to
        # the type's declared SQL/graph targets. We pass ALL chunks (NOT the
        # routes_to_sql() filter, which is a quarterly_report-era per-chunk gate):
        # registry types declare targets at the type level, and extract_for_type's
        # Pydantic schema + confidence filter already does the precision filtering.
        extracted = self.sql_extractor.extract_for_type(chunks, doc_type, profile=profile)
        if extracted:
            self._write_typed_data(extracted, chunks, source_file, doc_type)

    def _write_typed_data(self, extracted: dict, chunks: list[Chunk], source_file: str, doc_type) -> None:
        """Route schema-extracted rows to the registry type's SQL targets, then
        derive graph nodes from the same extracted dict. Graph failures are
        logged but never fail the SQL+vector writes."""
        chunk_id = chunks[0].chunk_id  # representative chunk for source reference

        # Normalize council member names before SQL insert and graph derivation so
        # both stores use canonical names and the member set dedupes correctly.
        for v in extracted.get("votes", []):
            if v.get("council_member"):
                v["council_member"] = normalize_person_name(v["council_member"])

        # SQL — only keys the type declares as sql_targets; insert methods ignore
        # extra dict keys (e.g. source_text/confidence) via explicit column lists.
        if "resolutions" in doc_type.sql_targets and extracted.get("resolutions"):
            self.sql_store.insert_resolution_rows(extracted["resolutions"], chunk_id, source_file)
        if "votes" in doc_type.sql_targets and extracted.get("votes"):
            self.sql_store.insert_vote_rows(extracted["votes"], chunk_id, source_file)

        # Graph — derived from the SAME extracted dict (resolutions/votes → nodes).
        try:
            resolutions = extracted.get("resolutions", [])
            if resolutions:
                self.graph_store.upsert_resolutions(resolutions)
            votes = extracted.get("votes", [])
            members = [{"name": m} for m in {
                v["council_member"] for v in votes if v.get("council_member")
            }]
            if members:
                self.graph_store.upsert_council_members(members)
            if votes:
                self.graph_store.upsert_votes(votes)
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
