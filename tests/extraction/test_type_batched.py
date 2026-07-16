import json
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion.schema_compiler import compile_type_schema
from src.models import DocumentType


class _FakeMsg:
    def __init__(self, t): self.content = [type("C", (), {"text": t})()]


class _SeqClient:
    def __init__(self, payloads): self._p = list(payloads); self._i = 0
    class _M:
        def __init__(self, o): self._o = o
        def create(self, **k):
            o = self._o; p = o._p[min(o._i, len(o._p) - 1)]; o._i += 1
            return _FakeMsg(p)
    @property
    def messages(self): return _SeqClient._M(self)


class _C:
    def __init__(self, t): self.text = t


def _boards_type():
    schema = compile_type_schema("boards", [{"name": "board_members", "proposed_columns": [
        {"name": "board_name", "type": "VARCHAR(255)"},
        {"name": "is_vacant", "type": "BOOLEAN"}]}])
    return DocumentType(name="boards", description="", sql_targets=["board_members"],
                        extraction_schema=schema)


def test_extract_type_batched_merges_dedups_and_strips():
    dt = _boards_type()
    b1 = json.dumps({"board_members": [
        {"board_name": "Audit", "is_vacant": False, "source_text": "x", "confidence": "high"}]})
    b2 = json.dumps({"board_members": [
        {"board_name": "Audit", "is_vacant": False, "source_text": "x", "confidence": "high"},  # dup across boundary
        {"board_name": "Plumbing", "is_vacant": True, "source_text": "y", "confidence": "high"}]})
    from src.config import Settings
    cfg = Settings(); cfg.extraction_batch_size = 1     # force 2 batches
    ext = SQLExtractor(settings=cfg, llm=_SeqClient([b1, b2]))
    out = ext.extract_type_batched([_C("a"), _C("b")], dt)
    assert sorted(r["board_name"] for r in out["board_members"]) == ["Audit", "Plumbing"]
    row = out["board_members"][0]
    assert "source_text" not in row and "confidence" not in row


def test_extract_type_batched_keeps_only_sql_targets_and_is_safe():
    dt = _boards_type()
    # extra table the type doesn't declare must be dropped; empty/bad batch is safe
    payload = json.dumps({"board_members": [{"board_name": "X", "source_text": "s", "confidence": "high"}],
                          "not_a_target": [{"foo": 1, "source_text": "s", "confidence": "high"}]})
    ext = SQLExtractor(llm=_SeqClient([payload]))
    out = ext.extract_type_batched([_C("a")], dt)
    assert list(out.keys()) == ["board_members"]
    assert SQLExtractor(llm=_SeqClient(["garbage"])).extract_type_batched([_C("a")], dt) == {}
