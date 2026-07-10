from src.ingestion import pipeline as P
from src.config import get_settings
from src.ingestion.registry import get_document_type
from src.models import Chunk, ChunkMetadata


def _chunk(i=0):
    m = ChunkMetadata(source_file="r.pdf", department="Bureau of Police",
                      document_type="quarterly_report", quarter="Q1", year=2025,
                      section="s", content_type="narrative", page_number=1,
                      parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=i, total_chunks_in_doc=1)
    return Chunk(text="Patrol Officer- (25)", metadata=m)


class _FakeStore:
    def __init__(self): self.calls = {}
    def _rec(self, name, rows): self.calls[name] = rows
    def insert_expenditure_rows(self, r, c, f): self._rec("expenditures", r)
    def insert_metric_rows(self, r, c, f): self._rec("metrics", r)
    def insert_grant_rows(self, r, c, f): self._rec("grants", r)
    def insert_vacancy_rows(self, r, c): self._rec("vacancies", r)
    def insert_goal_rows(self, r, c, f): self._rec("goals", r)
    def insert_project_rows(self, r, c, f): self._rec("projects", r)


class _FakeVector:
    def upsert_chunks(self, chunks): pass


class _FakeExtractor:
    def extract_quarterly(self, chunks, department="", quarter="", year=None):
        return {"vacancies": [{"position_title": "Patrol Officer", "status": "open",
                               "count": 25, "department": department, "quarter": quarter, "year": year}],
                "projects": [{"project_name": "Porch Lights", "department": department}]}


class _Profile:
    department = "Bureau of Police"; period = "Q1 2025"


def test_quarterly_branch_routes_all_targets():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.cfg = get_settings()
    pipe.vector_store = _FakeVector()
    pipe.sql_store = _FakeStore()
    pipe.sql_extractor = _FakeExtractor()
    pipe.graph_extractor = None  # no graph chunks here, so graph path is skipped
    dt = get_document_type("quarterly_report")
    pipe._store_chunks([_chunk()], "r.pdf", dt, quarantined=False, profile=_Profile())
    assert pipe.sql_store.calls["vacancies"][0]["count"] == 25
    assert pipe.sql_store.calls["projects"][0]["project_name"] == "Porch Lights"
