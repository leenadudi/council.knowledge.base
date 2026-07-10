import json
from src.extraction.sql_extractor import SQLExtractor
from src.models import Chunk, ChunkMetadata


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _SeqClient:
    """Returns a different payload per call (one per batch)."""
    def __init__(self, payloads): self._p = list(payloads); self._i = 0

    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k):
            o = self._o; p = o._p[min(o._i, len(o._p) - 1)]; o._i += 1
            return _FakeMsg(p)

    @property
    def messages(self): return _SeqClient._M(self)


def _chunk(text, i=0):
    m = ChunkMetadata(source_file="r.pdf", department="Bureau of Police",
                      document_type="quarterly_report", quarter="Q1", year=2025,
                      section="s", content_type="narrative", page_number=1,
                      parser_used="unstructured", ingestion_timestamp="t",
                      chunk_index=i, total_chunks_in_doc=2)
    return Chunk(text=text, metadata=m)


def test_extract_quarterly_merges_batches_tags_and_filters():
    batch1 = json.dumps({"vacancies": [
        {"position_title": "Patrol Officer", "status": "open", "count": 25,
         "source_text": "Patrol Officer- (25)", "confidence": "high"}],
        "projects": [{"project_name": "Porch Lights", "description": "pilot",
                      "status": "", "funding_source": "", "source_text": "Porch Lights", "confidence": "high"}]})
    batch2 = json.dumps({"goals": [
        {"goal_title": "Reduce response time", "description": "", "target": "", "status": "",
         "source_text": "Goal: reduce response time", "confidence": "high"}],
        "metrics": [{"metric_name": "cases", "metric_value": 52, "metric_unit": "count",
                     "source_text": "52 Cases", "confidence": "low"}]})
    from src.config import Settings
    cfg = Settings(); cfg.extraction_batch_size = 1  # isolated settings; force two batches
    ext = SQLExtractor(settings=cfg, llm=_SeqClient([batch1, batch2]))
    out = ext.extract_quarterly([_chunk("a", 0), _chunk("b", 1)],
                                department="Bureau of Police", quarter="Q1", year=2025)
    # merged across batches
    assert out["vacancies"][0]["position_title"] == "Patrol Officer"
    assert out["vacancies"][0]["count"] == 25
    assert out["projects"][0]["project_name"] == "Porch Lights"
    assert out["goals"][0]["goal_title"] == "Reduce response time"
    # low-confidence metric dropped
    assert "metrics" not in out or out["metrics"] == []
    # tagged with period, source_text stripped
    v = out["vacancies"][0]
    assert v["department"] == "Bureau of Police" and v["quarter"] == "Q1" and v["year"] == 2025
    assert "source_text" not in v and "confidence" not in v


def test_extract_quarterly_empty_and_bad_json_safe():
    assert SQLExtractor(llm=_SeqClient(["{}"])).extract_quarterly([]) == {}
    ext = SQLExtractor(llm=_SeqClient(["not json"]))
    assert ext.extract_quarterly([_chunk("x")]) == {}
