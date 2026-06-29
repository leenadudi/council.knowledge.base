import json
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _FakeClient:
    def __init__(self, payload): self._p = payload

    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k): return _FakeMsg(self._o._p)

    @property
    def messages(self): return _FakeClient._M(self)


def _chunk(text):
    m = ChunkMetadata(source_file="r.pdf", department="DEDBH", document_type="resolution",
                      quarter="", year=2026, section="RESOLVED", content_type="legal_authorization",
                      page_number=1, parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text=text, metadata=m)


def test_extract_for_type_resolution():
    payload = json.dumps({"resolutions": [{"resolution_number": "2026-R-12", "amount": 40000.0,
                          "vendor": "Acme", "source_text": "RESOLVED", "confidence": "high"}],
                          "votes": []})
    ext = SQLExtractor(llm=_FakeClient(payload))
    out = ext.extract_for_type([_chunk("RESOLVED ... $40,000 to Acme")], get_document_type("resolution"))
    assert out["resolutions"][0]["resolution_number"] == "2026-R-12"
    assert out["resolutions"][0]["amount"] == 40000.0


def test_extract_for_type_returns_empty_for_no_chunks():
    ext = SQLExtractor(llm=_FakeClient("{}"))
    out = ext.extract_for_type([], get_document_type("resolution"))
    assert out == {}


def test_extract_for_type_returns_empty_for_none_doc_type():
    ext = SQLExtractor(llm=_FakeClient("{}"))
    out = ext.extract_for_type([_chunk("text")], None)
    assert out == {}


def test_extract_for_type_filters_low_confidence():
    payload = json.dumps({"resolutions": [
        {"resolution_number": "2026-R-1", "amount": 1000.0, "vendor": "A",
         "source_text": "RESOLVED", "confidence": "high"},
        {"resolution_number": "2026-R-2", "amount": 2000.0, "vendor": "B",
         "source_text": "RESOLVED", "confidence": "low"},
    ], "votes": []})
    ext = SQLExtractor(llm=_FakeClient(payload))
    out = ext.extract_for_type([_chunk("RESOLVED ...")], get_document_type("resolution"))
    assert len(out["resolutions"]) == 1
    assert out["resolutions"][0]["resolution_number"] == "2026-R-1"


def test_extract_for_type_only_returns_sql_targets():
    """
    Genuine exercise of the `if k in doc_type.sql_targets` guard.

    The extraction schema has TWO valid fields — resolutions and audit_notes —
    but the DocumentType only declares resolutions as a sql_target.  Both fields
    are populated with high-confidence rows by the fake LLM.  The test asserts
    that audit_notes is dropped and resolutions is kept.  If the guard were
    removed, audit_notes would appear in the output and the second assertion
    would fail.
    """
    from pydantic import BaseModel as PydanticBase
    from src.models import DocumentType, ChunkingHints

    class _AuditRow(PydanticBase):
        note: str = ""
        confidence: str = "high"

    class _ResolutionRow(PydanticBase):
        resolution_number: str = ""
        confidence: str = "high"

    class _FilterTestSchema(PydanticBase):
        resolutions: list[_ResolutionRow] = []
        audit_notes: list[_AuditRow] = []

    filter_doc_type = DocumentType(
        name="filter_test",
        description="test doc type for sql_targets filter",
        sql_targets=["resolutions"],   # audit_notes intentionally excluded
        extraction_schema=_FilterTestSchema,
    )

    payload = json.dumps({
        "resolutions": [{"resolution_number": "2026-R-99", "confidence": "high"}],
        "audit_notes": [{"note": "should be dropped", "confidence": "high"}],
    })
    ext = SQLExtractor(llm=_FakeClient(payload))
    out = ext.extract_for_type([_chunk("RESOLVED ... Acme")], filter_doc_type)

    assert "resolutions" in out, "resolutions (a sql_target) must be present"
    assert "audit_notes" not in out, "audit_notes (not in sql_targets) must be filtered out"


def test_extract_for_type_returns_empty_for_none_schema():
    """extract_for_type returns {} when extraction_schema is None; no LLM call required."""
    from src.models import DocumentType

    no_schema_doc_type = DocumentType(
        name="no_schema",
        description="doc type with no extraction schema",
        sql_targets=["resolutions"],
        extraction_schema=None,
    )
    # Pass a client that would raise if called, to confirm no LLM call is made.
    class _ErrorClient:
        @property
        def messages(self):
            raise AssertionError("LLM must not be called when extraction_schema is None")

    ext = SQLExtractor(llm=_ErrorClient())
    out = ext.extract_for_type([_chunk("some text")], no_schema_doc_type)
    assert out == {}
