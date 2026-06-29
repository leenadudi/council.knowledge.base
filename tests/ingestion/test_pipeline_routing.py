"""Routing/quarantine unit tests for the ingestion pipeline.

Only ``_is_quarantined`` is exercised here: the full ``ingest_document`` /
``_store_chunks`` path requires live vector/SQL/graph clients and is covered by
manual review + integration runs, not by this unit suite.
"""
from src.config import get_settings
from src.models import DocumentProfile


def test_is_quarantined():
    from src.ingestion import pipeline as P

    # Skip __init__ (which builds DB clients); __new__ leaves cfg unset, so we
    # must set it ourselves — _is_quarantined reads cfg.profile_confidence_threshold.
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    pipe.cfg = get_settings()

    # Low-confidence known type -> quarantined.
    assert pipe._is_quarantined(
        DocumentProfile(document_type="resolution", department="X", confidence=0.10)
    ) is True

    # High-confidence known type -> NOT quarantined.
    assert pipe._is_quarantined(
        DocumentProfile(document_type="resolution", department="X", confidence=0.90)
    ) is False

    # "unclassified" is always quarantined, even at high confidence.
    assert pipe._is_quarantined(
        DocumentProfile(document_type="unclassified", department="", confidence=0.99)
    ) is True
