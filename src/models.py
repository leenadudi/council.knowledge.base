"""Shared data models for the Harrisburg Knowledge Base."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Content classification
# ---------------------------------------------------------------------------

CONTENT_TYPES = ("narrative", "table", "metrics", "org_data", "project", "header")

# Which stores receive each content type
STORAGE_ROUTING: dict[str, dict[str, bool]] = {
    "narrative": {"vector": True, "sql": False, "graph": False},
    "table":     {"vector": True, "sql": True,  "graph": True},
    "metrics":   {"vector": True, "sql": True,  "graph": False},
    "org_data":  {"vector": True, "sql": False, "graph": True},
    "project":   {"vector": True, "sql": False, "graph": True},
    "header":    {"vector": True, "sql": False, "graph": False},
}


# ---------------------------------------------------------------------------
# Agentic ingestion models
# ---------------------------------------------------------------------------

class DocumentProfile(BaseModel):
    document_type: str
    department: str
    period: str = ""                       # "Q1 2026", "2026", an adopted date, etc.
    title: str = ""
    identifying_ids: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    proposed_type: Optional[str] = None    # set when the agent thinks it's a new, unknown type


class ChunkingHints(BaseModel):
    keep_together: list[str] = Field(default_factory=list)   # marker words whose blocks must not split
    section_headers: Optional[list[str]] = None              # override default section names; None = use defaults


class DocumentType(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    name: str
    description: str
    identifying_signals: list[str] = Field(default_factory=list)
    content_vocab: list[str] = Field(default_factory=list)
    sql_targets: list[str] = Field(default_factory=list)
    graph_targets: list[str] = Field(default_factory=list)
    chunking: ChunkingHints = Field(default_factory=ChunkingHints)
    extraction_schema: Optional[type] = None    # Pydantic model class used as the LLM extraction contract
    metadata_schema: Optional[type] = None
    anchor_field: Optional[str] = None   # identifier that uniquely keys the single primary record in a one-record-per-document type (e.g. "resolution_number")


# ---------------------------------------------------------------------------
# Chunk — the unit of ingestion
# ---------------------------------------------------------------------------

@dataclass
class ChunkMetadata:
    source_file: str
    department: str
    document_type: str
    section: str
    content_type: str
    page_number: int
    parser_used: str
    ingestion_timestamp: str
    chunk_index: int
    total_chunks_in_doc: int
    quarter: str = ""
    year: Optional[int] = None
    needs_review: bool = False

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "department": self.department,
            "document_type": self.document_type,
            "quarter": self.quarter,
            "year": self.year,
            "section": self.section,
            "content_type": self.content_type,
            "page_number": self.page_number,
            "parser_used": self.parser_used,
            "ingestion_timestamp": self.ingestion_timestamp,
            "chunk_index": self.chunk_index,
            "total_chunks_in_doc": self.total_chunks_in_doc,
            "needs_review": self.needs_review,
        }


@dataclass
class Chunk:
    text: str
    metadata: ChunkMetadata
    chunk_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    embedding: Optional[list[float]] = None

    @property
    def content_type(self) -> str:
        return self.metadata.content_type

    def routes_to_sql(self) -> bool:
        return STORAGE_ROUTING.get(self.content_type, {}).get("sql", False)

    def routes_to_graph(self) -> bool:
        return STORAGE_ROUTING.get(self.content_type, {}).get("graph", False)


# ---------------------------------------------------------------------------
# Parsed document element (before chunking)
# ---------------------------------------------------------------------------

@dataclass
class ParsedElement:
    """A single element extracted from a document by a parser."""
    element_type: str       # Title, NarrativeText, Table, ListItem, Header, etc.
    text: str
    page_number: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    source_file: str
    parser_used: str        # "unstructured" | "vision_llm"
    elements: list[ParsedElement]
    total_pages: int


# ---------------------------------------------------------------------------
# Query pipeline models
# ---------------------------------------------------------------------------

class QueryPlan(BaseModel):
    sources: list[str] = Field(description="Stores to query: sql, vector, graph")
    execution: str = Field(description="parallel or sequential")
    sequential_order: Optional[list[str]] = None
    sql_query: Optional[str] = None
    vector_query: Optional[str] = None
    graph_query: Optional[str] = None
    metadata_filters: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""
    resolved_question: Optional[str] = None


class RetrievalResult(BaseModel):
    store: str
    chunks: list[dict[str, Any]] = Field(default_factory=list)
    sql_rows: list[dict[str, Any]] = Field(default_factory=list)
    graph_data: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class Citation(BaseModel):
    source_file: str
    department: Optional[str] = None
    section: Optional[str] = None
    quarter: Optional[str] = None
    year: Optional[int] = None
    chunk_id: Optional[str] = None


class QueryResponse(BaseModel):
    query_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    stores_queried: list[str] = Field(default_factory=list)
    retrieval_score: Optional[float] = None
    total_time_ms: Optional[int] = None
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ---------------------------------------------------------------------------
# Evaluation models
# ---------------------------------------------------------------------------

class EvaluationScore(BaseModel):
    retrieval_score: float = Field(ge=1, le=5)
    accuracy_score: float = Field(ge=1, le=5)
    completeness_score: float = Field(ge=1, le=5)
    retrieval_failure: bool = False
    hallucination_detected: bool = False
    reasoning: str = ""


class EvaluationSuiteEntry(BaseModel):
    id: Optional[int] = None
    question: str
    expected_answer: str
    store_type: str      # sql, vector, graph, cross
    department: Optional[str] = None
    quarter: Optional[str] = None
    year: Optional[int] = None


class EvaluationRunResult(BaseModel):
    run_id: str
    run_date: str
    question_id: Optional[int]
    question: str
    expected_answer: str
    actual_answer: str
    retrieval_score: float
    accuracy_score: float
    completeness_score: float
    passed: bool
    notes: str = ""


# ---------------------------------------------------------------------------
# User feedback
# ---------------------------------------------------------------------------

class UserFeedback(BaseModel):
    query_id: str
    feedback: str           # "positive" | "negative"
    failure_category: Optional[str] = None
    notes: Optional[str] = None
