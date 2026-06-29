from src.ingestion.metadata import build_chunk_metadata, filename_hint
from src.models import DocumentProfile


def test_metadata_comes_from_profile_not_filename():
    profile = DocumentProfile(document_type="resolution", department="DEDBH",
                              period="2026-03-03", title="RES", confidence=0.9)
    meta = build_chunk_metadata(
        chunk_dict={"section": "RESOLVED", "page_number": 1},
        source_file="whatever_random_name.pdf", chunk_index=0, total_chunks=3,
        content_type="legal_authorization", parser_used="unstructured", profile=profile,
    )
    assert meta["document_type"] == "resolution"
    assert meta["department"] == "DEDBH"
    assert meta["needs_review"] is False


def test_filename_hint_still_parses_quarterly_convention():
    hint = filename_hint("Misc. Documents - Quarterly Reports - 2026 - Bureau of Codes_Q1 2026.pdf")
    assert hint["quarter"] == "Q1"
    assert hint["year"] == 2026
