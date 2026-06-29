"""
Tests for bounded-concurrency ingestion: classify_batch and ingest_directory.
"""
import threading
import time
from src.ingestion.classifier import classify_batch
from src.config import get_settings


def test_classify_batch_preserves_order_and_runs_concurrently(monkeypatch):
    # Stub classify_chunk with a fake that tracks simultaneous concurrency
    from src.ingestion import classifier as C

    lock = threading.Lock()
    active_counter = [0]
    max_observed = [0]

    def fake(chunk_dict, element_type, client=None, settings=None, vocab=None):
        with lock:
            active_counter[0] += 1
            if active_counter[0] > max_observed[0]:
                max_observed[0] = active_counter[0]
        time.sleep(0.02)  # brief sleep so threads actually overlap
        with lock:
            active_counter[0] -= 1
        return chunk_dict["text"]  # echo so we can assert order

    monkeypatch.setattr(C, "classify_chunk", fake)

    cfg = get_settings()
    chunks = [{"text": f"c{i}"} for i in range(8)]
    out = classify_batch(chunks, ["NarrativeText"] * 8)

    assert out == [f"c{i}" for i in range(8)], "order must be preserved"
    assert max_observed[0] > 1, (
        f"expected concurrent execution but max_observed={max_observed[0]} (ran serially)"
    )
    assert max_observed[0] <= cfg.ingest_workers, (
        f"concurrency {max_observed[0]} exceeded cap {cfg.ingest_workers}"
    )


def test_classify_batch_empty_input():
    """Empty input returns empty list without error."""
    out = classify_batch([], [])
    assert out == []
