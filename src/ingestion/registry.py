"""Declared registry of document types. Adding a new type = adding an entry here
plus its Pydantic schema(s) and any new SQL tables / graph nodes. No pipeline edits."""
from __future__ import annotations

from src.models import DocumentType, ChunkingHints
from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
from src.ingestion.schemas.resolution import ResolutionExtraction
from src.ingestion.schemas.minutes import MinutesExtraction
from src.ingestion.schemas.legislation import LegislationExtraction
from src.ingestion.schemas.budget import BudgetExtraction

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

_MINUTES = DocumentType(
    name="minutes",
    description=("Official minutes of a City Council legislative session. Records the meeting "
                 "date, session type, presiding officer, roll-call attendance, and the actions "
                 "taken on resolutions and ordinances (read into record, committee referral, "
                 "final passage). Fixed agenda: CALL TO ORDER, ROLL CALL, COURTESY OF THE FLOOR, "
                 "APPROVAL OF MINUTES, ORDINANCES, RESOLUTIONS, NEW BUSINESS, ADJOURNMENT."),
    identifying_signals=["LEGISLATIVE SESSION", "CALL TO ORDER", "ROLL CALL",
                         "COURTESY OF THE FLOOR", "ADJOURNMENT", "APPROVAL OF MINUTES",
                         "ORDINANCES FOR FINAL PASSAGE"],
    content_vocab=["narrative", "roll_call", "agenda_action", "header"],
    sql_targets=["meetings", "meeting_actions"],
    graph_targets=[],
    chunking=ChunkingHints(),  # default section-aware chunking (clean native text)
    extraction_schema=MinutesExtraction,
)

_LEGISLATION = DocumentType(
    name="legislation",
    description=("A formal ordinance or bill considered/enacted by City Council — the twin of a "
                 "resolution but for LEGISLATION. Has a BILL NO., ordinance language "
                 "(AN ORDINANCE / BE IT ORDAINED), possibly amending the city code, a sponsor, "
                 "and an outcome status (introduced, amended, signed, vetoed, veto-overridden). "
                 "Distinguish from a resolution: a bill says 'BILL NO.' / 'ORDINANCE'; a "
                 "resolution says 'RESOLUTION NO.' / 'RESOLVED'."),
    identifying_signals=["BILL NO", "AN ORDINANCE", "BE IT ORDAINED", "ORDINANCE",
                         "AMENDING", "CERTIFICATE OF ACCEPTANCE"],
    content_vocab=["legal_authorization", "ordinance_clause", "narrative", "header"],
    sql_targets=["legislation"],
    graph_targets=[],
    chunking=ChunkingHints(keep_together=["ordained", "section"]),
    extraction_schema=LegislationExtraction,
    anchor_field="bill_number",
)

_BUDGET = DocumentType(
    name="budget",
    description=("A city budget document: the annual approved/proposed budget, a department or "
                 "bureau budget-hearing presentation, budget questions, or a budget veto. Contains "
                 "appropriations by department and fund, revenue, and fiscal-year figures. Layouts "
                 "vary widely; extract department-level appropriations only where a clean summary "
                 "table states them."),
    identifying_signals=["BUDGET", "Approved Budget", "Proposed Budget", "Appropriation",
                         "General Fund", "Budget Presentation", "Fiscal Year", "VETO"],
    content_vocab=["table", "narrative", "metrics", "header"],
    sql_targets=["appropriations"],
    graph_targets=[],
    chunking=ChunkingHints(),  # default section-aware chunking
    extraction_schema=BudgetExtraction,
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
register(_MINUTES)
register(_LEGISLATION)
register(_BUDGET)
KNOWN_TYPE_NAMES: list[str] = list(_REGISTRY.keys())  # recompute after all registrations
