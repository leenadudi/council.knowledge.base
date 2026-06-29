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
    # The resolution doc type only has resolutions and votes as sql_targets
    payload = json.dumps({"resolutions": [{"resolution_number": "2026-R-12", "amount": 40000.0,
                          "vendor": "Acme", "source_text": "RESOLVED", "confidence": "high"}],
                          "votes": [], "unexpected_key": [{"foo": "bar"}]})
    ext = SQLExtractor(llm=_FakeClient(payload))
    out = ext.extract_for_type([_chunk("RESOLVED ... $40,000 to Acme")], get_document_type("resolution"))
    assert "unexpected_key" not in out
    assert "resolutions" in out
