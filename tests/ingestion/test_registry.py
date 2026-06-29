# tests/ingestion/test_registry.py
import pytest
from src.ingestion.registry import get_document_type, all_document_types, KNOWN_TYPE_NAMES

def test_quarterly_report_registered():
    dt = get_document_type("quarterly_report")
    assert dt is not None
    assert "metrics" in dt.content_vocab or "table" in dt.content_vocab
    assert "expenditures" in dt.sql_targets

def test_unknown_type_returns_none():
    assert get_document_type("nonexistent_type") is None

def test_every_registered_type_is_wellformed():
    for dt in all_document_types():
        assert dt.name and dt.description
        assert dt.content_vocab, f"{dt.name} has empty content_vocab"
        assert dt.name in KNOWN_TYPE_NAMES
