from contextlib import contextmanager

from src.ingestion import pipeline as P
from src.models import Chunk, ChunkMetadata, DocumentType


def _chunk():
    m = ChunkMetadata(source_file="b.pdf", department="", document_type="boards",
                      quarter="", year=2026, section="s", content_type="narrative",
                      page_number=1, parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=0, total_chunks_in_doc=1)
    return Chunk(text="x", metadata=m)


class _Store:
    def __init__(self): self.dynamic = []
    @contextmanager
    def transaction(self):
        yield
    def insert_dynamic_rows(self, table, rows, cid, sf): self.dynamic.append((table, rows, sf))


class _Graph:
    def __getattr__(self, _): return lambda *a, **k: None


def test_data_driven_target_routes_to_generic_insert_and_stamps_roster_year():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.sql_store = _Store(); pipe.graph_store = _Graph()
    dt = DocumentType(name="board_memberships", description="", sql_targets=["board_members"])
    extracted = {"board_members": [
        {"board_name": "Audit Committee", "is_vacant": True, "roster_year": None}]}
    sf = "Misc. Documents - 2026 - Boards, Commissions, Authorities_2026 Booklet 1.pdf"
    pipe._write_typed_data(extracted, [_chunk()], sf, dt)

    assert len(pipe.sql_store.dynamic) == 1
    table, rows, out_sf = pipe.sql_store.dynamic[0]
    assert table == "board_members" and out_sf == sf
    assert rows[0]["roster_year"] == 2026            # stamped from the filename
    assert rows[0]["board_name"] == "Audit Committee"


def test_year_from_filename():
    assert P._year_from_filename("x 2026 Booklet 1.pdf") == 2026
    assert P._year_from_filename("no year here.pdf") is None
