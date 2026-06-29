from unittest.mock import MagicMock, patch

from src.ingestion.classifier import _validate_against_vocab, classify_chunk


def test_validate_against_vocab_passthrough():
    assert _validate_against_vocab("vote_record", ["legal_authorization", "vote_record"]) == "vote_record"


def test_validate_against_vocab_falls_back_to_first():
    assert _validate_against_vocab("garbage", ["legal_authorization", "vote_record"]) == "legal_authorization"


def test_validate_against_vocab_none_uses_content_types():
    from src.models import CONTENT_TYPES
    assert _validate_against_vocab("table", None) == "table"
    assert _validate_against_vocab("garbage", None) == "narrative"


def test_classify_chunk_vocab_skips_rule_based_and_validates():
    """
    When vocab is provided, classify_chunk must:
      (a) skip _rule_based entirely (even for text that would normally classify as "table"),
      (b) use _llm_classify and validate the result against the vocab.

    We use text with a very high numeric ratio (>0.60) — _rule_based would return "metrics"
    for this text if it were consulted.  With vocab supplied it must NOT be consulted.
    """
    vocab = ["legal_authorization", "vote_record"]

    # Fake client whose .messages.create() returns a valid vocab label
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(text="vote_record")]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg

    # Text that _rule_based would classify as "metrics" (numeric ratio > 0.60)
    highly_numeric_text = "1234 5678 9012 3456 7890 1234 5678 9012 3456 7890"

    chunk = {"text": highly_numeric_text}

    with patch("src.ingestion.classifier._rule_based") as mock_rule_based:
        result = classify_chunk(chunk, "NarrativeText", client=fake_client, vocab=vocab)

    # (a) _rule_based must never have been called
    mock_rule_based.assert_not_called()

    # (b) result must be the LLM value, which is inside the vocab
    assert result == "vote_record"

    # Also verify that an out-of-vocab LLM reply is coerced to vocab[0]
    fake_msg.content = [MagicMock(text="garbage")]
    with patch("src.ingestion.classifier._rule_based") as mock_rule_based2:
        result2 = classify_chunk(chunk, "NarrativeText", client=fake_client, vocab=vocab)
    mock_rule_based2.assert_not_called()
    assert result2 == "legal_authorization"
