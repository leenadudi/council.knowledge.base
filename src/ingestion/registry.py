"""Declared registry of document types. Adding a new type = adding an entry here
plus its Pydantic schema(s) and any new SQL tables / graph nodes. No pipeline edits."""
from __future__ import annotations

from src.models import DocumentType, ChunkingHints
from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
from src.ingestion.schemas.resolution import ResolutionExtraction

_QUARTERLY_REPORT = DocumentType(
    name="quarterly_report",
    description=("A city department's quarterly report: description, quarterly summary, "
                 "metrics/counts, budget/expenditure tables, annual goals, special projects, "
                 "vacancies, community engagement."),
    identifying_signals=["quarterly report", "Q1", "Q2", "Q3", "Q4", "year-to-date", "annual goals"],
    content_vocab=["narrative", "table", "metrics", "org_data", "project", "header"],
    sql_targets=["expenditures", "metrics", "grants", "vacancies"],
    graph_targets=["Department", "Person", "Project", "Grant"],
    chunking=ChunkingHints(),  # use default section-aware chunking
    extraction_schema=QuarterlyReportExtraction,
)

_RESOLUTION = DocumentType(
    name="resolution",
    description=("A formal City Council action authorizing a contract, expenditure, or policy. "
                 "Has a RESOLUTION NO., WHEREAS reasoning clauses, a RESOLVED authorization, "
                 "an adoption date, and a vote record by council member."),
    identifying_signals=["RESOLUTION NO", "WHEREAS", "RESOLVED", "BE IT RESOLVED"],
    content_vocab=["legal_authorization", "whereas_clause", "vote_record", "narrative", "header"],
    sql_targets=["resolutions", "votes"],
    graph_targets=["Resolution", "Vendor", "CouncilMember"],
    chunking=ChunkingHints(keep_together=["whereas", "resolved"]),
    extraction_schema=ResolutionExtraction,
    anchor_field="resolution_number",
)

_REGISTRY: dict[str, DocumentType] = {
    _QUARTERLY_REPORT.name: _QUARTERLY_REPORT,
}


def register(dt: DocumentType) -> None:
    _REGISTRY[dt.name] = dt


def get_document_type(name: str) -> DocumentType | None:
    return _REGISTRY.get(name)


def all_document_types() -> list[DocumentType]:
    return list(_REGISTRY.values())


register(_RESOLUTION)
KNOWN_TYPE_NAMES: list[str] = list(_REGISTRY.keys())  # recompute after all registrations
