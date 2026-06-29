"""
Tests for bounded-concurrency ingestion: classify_batch and ingest_directory.
"""
import time
from src.ingestion.classifier import classify_batch


def test_classify_batch_preserves_order_and_runs_concurrently(monkeypatch):
    # stub classify_chunk to sleep, so concurrency is observable and order must hold
    from src.ingestion import classifier as C

    def fake(chunk_dict, element_type, client=None, settings=None, vocab=None):
        time.sleep(0.05)
        return chunk_dict["text"]  # echo so we can assert order

    monkeypatch.setattr(C, "classify_chunk", fake)
    chunks = [{"text": f"c{i}"} for i in range(8)]
    t0 = time.time()
    out = classify_batch(chunks, ["NarrativeText"] * 8)
    elapsed = time.time() - t0
    assert out == [f"c{i}" for i in range(8)]          # order preserved
    assert elapsed < 0.05 * 8 * 0.7                    # ran concurrently, not serially (margin: 0.7x)


def test_classify_batch_empty_input():
    """Empty input returns empty list without error."""
    out = classify_batch([], [])
    assert out == []
