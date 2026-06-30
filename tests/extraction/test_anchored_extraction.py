import json
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata, DocumentProfile


def _chunk(text):
    m = ChunkMetadata(source_file="r.pdf", department="DEDBH", document_type="resolution",
                      quarter="", year=2026, section="", content_type="legal_authorization",
                      page_number=1, parser_used="vision_llm", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text=text, metadata=m)


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _FakeClient:
    def __init__(self, payload): self._p = payload; self.last_prompt = None

    class _M:
        def __init__(self, o): self._o = o

        def create(self, **k):
            self._o.last_prompt = k["messages"][0]["content"]
            return _FakeMsg(self._o._p)

    @property
    def messages(self): return _FakeClient._M(self)


# payload mimics the real contamination: a bogus extra resolution alongside the right one
_PAYLOAD = json.dumps({
    "resolutions": [
        {"resolution_number": "7-2026", "amount": 19500.0, "vendor": "Floura Teeter",
         "source_text": "x", "confidence": "high"},
        {"resolution_number": "9-2026", "amount": 3000000.0, "vendor": "US DOT",
         "source_text": "y", "confidence": "high"},
    ],
    "votes": [{"resolution_number": "9-2026", "council_member": "Smith", "vote": "yes",
               "source_text": "z", "confidence": "high"}],
})


def _profile():
    return DocumentProfile(document_type="resolution", department="DEDBH",
                           period="2026-02-10", identifying_ids={"resolution_number": "9-2026"},
                           confidence=0.9)


def test_anchor_block_includes_resolution_number():
    c = _FakeClient(_PAYLOAD)
    SQLExtractor(llm=c).extract_for_type([_chunk("...")], get_document_type("resolution"), profile=_profile())
    assert "9-2026" in c.last_prompt and "SINGLE" in c.last_prompt


def test_guard_collapses_to_single_keyed_row():
    out = SQLExtractor(llm=_FakeClient(_PAYLOAD)).extract_for_type(
        [_chunk("...")], get_document_type("resolution"), profile=_profile())
    assert len(out["resolutions"]) == 1
    assert out["resolutions"][0]["resolution_number"] == "9-2026"
    assert out["resolutions"][0]["amount"] == 3000000.0     # kept the matching row, not the bogus 7-2026
    assert len(out["votes"]) == 1                            # votes not collapsed


def test_no_anchoring_without_profile():
    out = SQLExtractor(llm=_FakeClient(_PAYLOAD)).extract_for_type(
        [_chunk("...")], get_document_type("resolution"), profile=None)
    assert len(out["resolutions"]) == 2                      # unchanged behavior


def test_guard_fallback_forces_anchor_on_rows_0_when_no_match():
    # Neither row matches "9-2026", so the guard falls back to rows[0] and forces the field.
    _PAYLOAD_NO_MATCH = json.dumps({
        "resolutions": [
            {"resolution_number": "1-2026", "amount": 500.0, "vendor": "ACME",
             "source_text": "a", "confidence": "high"},
            {"resolution_number": "2-2026", "amount": 700.0, "vendor": "CORP",
             "source_text": "b", "confidence": "high"},
        ],
        "votes": [],
    })
    out = SQLExtractor(llm=_FakeClient(_PAYLOAD_NO_MATCH)).extract_for_type(
        [_chunk("...")], get_document_type("resolution"), profile=_profile())
    assert len(out["resolutions"]) == 1
    assert out["resolutions"][0]["resolution_number"] == "9-2026"
