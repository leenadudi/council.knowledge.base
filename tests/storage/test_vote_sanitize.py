from src.storage.sql_store import sanitize_vote


def test_normal_vote_unchanged():
    assert sanitize_vote("yes") == "yes"


def test_overlong_vote_truncated_to_50():
    v = sanitize_vote("affirmative with a very long qualifying explanation attached here")
    assert v is not None and len(v) <= 50


def test_none_stays_none():
    assert sanitize_vote(None) is None


def test_whitespace_trimmed():
    assert sanitize_vote("  yes  ") == "yes"
