from src.ingestion import pipeline as P
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata


def _chunk():
    m = ChunkMetadata(source_file="r.pdf", department="X", document_type="resolution",
                      quarter="", year=2026, section="", content_type="legal_authorization",
                      page_number=1, parser_used="vision_llm", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text="t", metadata=m)


class _FakeSQL:
    def __init__(self): self.votes = None; self.res = None
    def insert_resolution_rows(self, rows, cid, sf): self.res = rows
    def insert_vote_rows(self, rows, cid, sf): self.votes = rows


class _FakeGraph:
    def __init__(self): self.members = None; self.votes = None; self.res = None
    def upsert_resolutions(self, r): self.res = r
    def upsert_council_members(self, m): self.members = m
    def upsert_votes(self, v): self.votes = v


def test_write_typed_data_normalizes_member_names():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.sql_store = _FakeSQL(); pipe.graph_store = _FakeGraph()
    extracted = {
        "resolutions": [{"resolution_number": "9-2026", "amount": 1.0}],
        "votes": [
            {"resolution_number": "9-2026", "council_member": "Councilman Jones", "vote": "yes"},
            {"resolution_number": "9-2026", "council_member": "  JONES ", "vote": "yes"},
        ],
    }
    pipe._write_typed_data(extracted, [_chunk()], "r.pdf", get_document_type("resolution"))
    # both vote rows normalized to "Jones"
    assert all(v["council_member"] == "Jones" for v in pipe.sql_store.votes)
    # graph members deduped to a single normalized "Jones"
    assert [m["name"] for m in pipe.graph_store.members] == ["Jones"]
