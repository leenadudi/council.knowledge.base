# tests/ingestion/test_resolution_schema.py
from src.ingestion.registry import get_document_type
from src.ingestion.schemas.resolution import ResolutionExtraction


def test_resolution_registered_with_keep_together():
    dt = get_document_type("resolution")
    assert dt is not None
    assert "resolutions" in dt.sql_targets and "votes" in dt.sql_targets
    assert dt.chunking.keep_together == ["whereas", "resolved"]
    assert dt.extraction_schema is ResolutionExtraction


def test_resolution_extraction_parses():
    e = ResolutionExtraction.model_validate({
        "resolutions": [{"resolution_number": "2026-R-12", "title": "Award",
                         "amount": 40000.0, "vendor": "Acme", "adopted_date": "2026-03-03",
                         "status": "adopted", "source_text": "RESOLVED...", "confidence": "high"}],
        "votes": [{"resolution_number": "2026-R-12", "council_member": "Smith",
                   "vote": "yes", "source_text": "Smith - yes", "confidence": "high"}],
    })
    assert e.resolutions[0].amount == 40000.0
    assert e.votes[0].vote == "yes"
