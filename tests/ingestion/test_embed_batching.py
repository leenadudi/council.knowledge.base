# tests/ingestion/test_embed_batching.py
"""Regression: embedding must batch under Voyage's 1000-input limit so large
documents (e.g. the 1,100-chunk annual budget) don't fail to ingest."""
from src.ingestion.pipeline import IngestionPipeline
from src.config import get_settings


class _FakeResp:
    def __init__(self, n): self.embeddings = [[0.0, 0.1] for _ in range(n)]


class _FakeVoyage:
    def __init__(self): self.batch_sizes = []
    def embed(self, texts, model=None):
        self.batch_sizes.append(len(texts))
        return _FakeResp(len(texts))


class _DummyChunk:
    def __init__(self, t): self.text = t; self.embedding = None


def _pipeline_with_fake_voyage():
    p = IngestionPipeline.__new__(IngestionPipeline)   # skip __init__ (no real API client)
    p.cfg = get_settings()
    p._voyage = _FakeVoyage()
    return p


def test_embed_chunks_batches_under_voyage_limit():
    p = _pipeline_with_fake_voyage()
    chunks = [_DummyChunk(f"chunk {i}") for i in range(1100)]   # the size that crashed
    p._embed_chunks(chunks)
    assert max(p._voyage.batch_sizes) <= 1000              # never exceeds Voyage's cap
    assert sum(p._voyage.batch_sizes) == 1100              # every chunk embedded exactly once
    assert all(c.embedding is not None for c in chunks)    # all chunks got their vector


def test_embed_chunks_small_doc_single_batch():
    p = _pipeline_with_fake_voyage()
    chunks = [_DummyChunk(f"c{i}") for i in range(10)]
    p._embed_chunks(chunks)
    assert p._voyage.batch_sizes == [10]                   # small docs still one call
    assert all(c.embedding == [0.0, 0.1] for c in chunks)
