from contextlib import contextmanager

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
    def __init__(self): self.calls = {}; self.files = {}; self.flags = []; self.txn_depth = 0
    def _rec(self, name, rows, f=None): self.calls[name] = rows; self.files[name] = f
    def insert_expenditure_rows(self, r, c, f): self._rec("expenditures", r, f)
    def insert_metric_rows(self, r, c, f): self._rec("metrics", r, f)
    def insert_grant_rows(self, r, c, f): self._rec("grants", r, f)
    def insert_vacancy_rows(self, r, c, f): self._rec("vacancies", r, f)
    def insert_goal_rows(self, r, c, f): self._rec("goals", r, f)
    def insert_project_rows(self, r, c, f): self._rec("projects", r, f)
    def insert_review_flag(self, source_file, stage, reason, detail=""):
        self.flags.append((source_file, stage, reason))

    @contextmanager
    def transaction(self):
        # Assert the structured writes happen INSIDE a transaction (atomicity).
        self.txn_depth += 1
        try:
            yield
        finally:
            self.txn_depth -= 1
        self.committed_inside = True


class _FakeVector:
    def upsert_chunks(self, chunks): pass


class _FakeExtractor:
    def extract_quarterly(self, chunks, department="", quarter="", year=None):
        return {"vacancies": [{"position_title": "Patrol Officer", "status": "open",
                               "count": 25, "department": department, "quarter": quarter, "year": year}],
                "projects": [{"project_name": "Porch Lights", "department": department}]}


class _EmptyExtractor:
    def extract_quarterly(self, chunks, department="", quarter="", year=None):
        return {}  # pass produced nothing


class _Profile:
    department = "Bureau of Police"; period = "Q1 2025"


def _pipe(extractor):
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.cfg = get_settings()
    pipe.vector_store = _FakeVector()
    pipe.sql_store = _FakeStore()
    pipe.sql_extractor = extractor
    pipe.graph_extractor = None  # no graph chunks here, so graph path is skipped
    return pipe


def test_quarterly_branch_routes_all_targets():
    pipe = _pipe(_FakeExtractor())
    dt = get_document_type("quarterly_report")
    pipe._store_chunks([_chunk()], "r.pdf", dt, quarantined=False, profile=_Profile())
    assert pipe.sql_store.calls["vacancies"][0]["count"] == 25
    assert pipe.sql_store.calls["projects"][0]["project_name"] == "Porch Lights"
    # vacancies now gets source_file like every other table (re-ingest orphan fix)
    assert pipe.sql_store.files["vacancies"] == "r.pdf"
    # writes happened inside a transaction (atomicity)
    assert getattr(pipe.sql_store, "committed_inside", False)


def test_quarterly_empty_extraction_raises_review_flag():
    pipe = _pipe(_EmptyExtractor())
    dt = get_document_type("quarterly_report")
    pipe._store_chunks([_chunk()], "r.pdf", dt, quarantined=False, profile=_Profile())
    # no rows written, but a review flag was raised (no silent data loss)
    assert pipe.sql_store.calls == {}
    assert any(stage == "validate" and "no structured rows" in reason
               for _, stage, reason in pipe.sql_store.flags)
