from src.ingestion.classifier import _validate_against_vocab


def test_validate_against_vocab_passthrough():
    assert _validate_against_vocab("vote_record", ["legal_authorization", "vote_record"]) == "vote_record"


def test_validate_against_vocab_falls_back_to_first():
    assert _validate_against_vocab("garbage", ["legal_authorization", "vote_record"]) == "legal_authorization"


def test_validate_against_vocab_none_uses_content_types():
    from src.models import CONTENT_TYPES
    assert _validate_against_vocab("table", None) == "table"
    assert _validate_against_vocab("garbage", None) == "narrative"
