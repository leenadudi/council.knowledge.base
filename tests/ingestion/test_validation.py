from src.ingestion.validation import validate_extraction


def _res(num, votes=3):
    return {
        "resolutions": [{"resolution_number": num, "vendor": "Acme", "title": "x"}],
        "votes": [{"resolution_number": num, "council_member": f"M{i}", "vote": "yes"}
                  for i in range(votes)],
    }


def test_valid_resolution_passes():
    assert validate_extraction("resolution", _res("21-2026")) == []


def test_number_equal_to_year_rejected():
    problems = validate_extraction("resolution", _res("2026-2026"))
    assert any("2026-2026" in p for p in problems)


def test_malformed_number_rejected():
    problems = validate_extraction("resolution", _res("../-2026"))
    assert problems  # non-empty


def test_missing_resolution_row_rejected():
    problems = validate_extraction("resolution", {"resolutions": [], "votes": []})
    assert problems


def test_implausible_vote_count_rejected():
    problems = validate_extraction("resolution", _res("21-2026", votes=40))
    assert any("vote" in p.lower() for p in problems)


def test_unknown_type_is_noop():
    assert validate_extraction("mystery", {"anything": []}) == []
