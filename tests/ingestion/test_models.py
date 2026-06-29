from src.models import DocumentProfile, ChunkingHints
from src.config import get_settings

def test_document_profile_defaults():
    p = DocumentProfile(
        document_type="resolution", department="DEDBH", period="2026",
        title="Resolution 2026-R-12", identifying_ids={"resolution_number": "2026-R-12"},
        confidence=0.91,
    )
    assert p.proposed_type is None
    assert p.identifying_ids["resolution_number"] == "2026-R-12"

def test_chunking_hints_defaults():
    h = ChunkingHints()
    assert h.keep_together == []
    assert h.section_headers is None

def test_settings_have_profiler_defaults():
    cfg = get_settings()
    assert cfg.profiler_model == "claude-haiku-4-5"
    assert cfg.ingest_workers >= 1
    assert 0.0 < cfg.profile_confidence_threshold <= 1.0
