import json
from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
from src.ingestion.registry import get_document_type


def test_schema_has_all_six_targets():
    fields = QuarterlyReportExtraction.model_fields
    assert set(fields) == {"expenditures", "metrics", "grants", "vacancies", "goals", "projects"}


def test_vacancy_row_has_count_and_project_row_present():
    schema = json.dumps(QuarterlyReportExtraction.model_json_schema())
    assert "count" in schema and "project_name" in schema and "funding_source" in schema


def test_registry_targets_updated():
    dt = get_document_type("quarterly_report")
    assert dt.sql_targets == ["expenditures", "metrics", "grants", "vacancies", "goals", "projects"]
