"""Contract for the ingest-side triage agent: does an unclassified doc contain
structured data worth storing, and where should each record-type go — an existing
table (with a column mapping) or a proposed new table?"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class ProposedColumn(BaseModel):
    name: str
    type: str = Field(description="one of TEXT, VARCHAR(n), INTEGER, DECIMAL(15,2), DATE, BOOLEAN")
    description: str = ""


class RecordTypeProposal(BaseModel):
    name: str
    description: str = ""
    target: str = Field(description='"existing" or "new"')
    existing_table: Optional[str] = None
    column_mapping: Optional[dict[str, str]] = None      # doc_field -> existing column
    proposed_columns: Optional[list[ProposedColumn]] = None
    match_confidence: float = 0.0
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


class TriageResult(BaseModel):
    has_structured_data: bool = False
    proposed_type_name: str = ""
    record_types: list[RecordTypeProposal] = Field(default_factory=list)
