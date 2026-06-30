from src.ingestion.names import normalize_person_name


def test_trims_and_collapses_whitespace():
    assert normalize_person_name("  john   smith ") == "John Smith"


def test_strips_leading_titles():
    assert normalize_person_name("Councilman Jones") == "Jones"
    assert normalize_person_name("Council Member O'Brien") == "O'Brien"
    assert normalize_person_name("Vice President Smith") == "Smith"
    assert normalize_person_name("Dr. Patel") == "Patel"


def test_case_folds():
    assert normalize_person_name("SMITH") == "Smith"


def test_empty_and_none():
    assert normalize_person_name("") == ""
    assert normalize_person_name(None) == ""
