from src.ingestion import pipeline as P
from src.config import get_settings
from src.models import Chunk, ChunkMetadata


def _chunk():
    m = ChunkMetadata(source_file="b.pdf", department="", document_type="unclassified",
                      quarter="", year=2026, section="s", content_type="narrative",
                      page_number=1, parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text="Audit Committee members: Ed Jaroch ...", metadata=m)


class _Vec:
    def upsert_chunks(self, chunks): pass


class _Store:
    def __init__(self): self.proposals = []
    def insert_type_proposal(self, sf, pt, payload): self.proposals.append((sf, pt, payload))


class _Profile:
    document_type = "unclassified"; proposed_type = "boards_commissions"; confidence = 0.3
    department = ""; period = ""


def _pipe():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.cfg = get_settings(); pipe.cfg.enable_triage = True
    pipe.vector_store = _Vec(); pipe.sql_store = _Store(); pipe.triage_llm = object()
    return pipe


def test_unclassified_doc_with_structured_data_creates_proposal(monkeypatch):
    from src.ingestion.schemas.triage import TriageResult, RecordTypeProposal
    pipe = _pipe()
    monkeypatch.setattr(P, "schema_summary", lambda store: "grants(department)")
    monkeypatch.setattr(P, "run_triage", lambda text, schema, llm=None:
        TriageResult(has_structured_data=True, proposed_type_name="boards_commissions",
                     record_types=[RecordTypeProposal(name="board_member", target="new",
                                                      match_confidence=0.9)]))
    pipe._store_chunks([_chunk()], "b.pdf", None, quarantined=True, profile=_Profile())
    assert pipe.sql_store.proposals and pipe.sql_store.proposals[0][1] == "boards_commissions"


def test_unclassified_without_structured_data_no_proposal(monkeypatch):
    from src.ingestion.schemas.triage import TriageResult
    pipe = _pipe()
    monkeypatch.setattr(P, "schema_summary", lambda store: "grants(department)")
    monkeypatch.setattr(P, "run_triage", lambda text, schema, llm=None: TriageResult())
    pipe._store_chunks([_chunk()], "b.pdf", None, quarantined=True, profile=_Profile())
    assert pipe.sql_store.proposals == []
