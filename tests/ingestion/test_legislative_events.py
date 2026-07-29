"""Unit tests for the deterministic legislative-event normalizer.

Fixture strings are real observed values from meeting_actions/legislation —
the normalizer itself must stay city-agnostic (keyword rules only).
"""
import pytest

from src.ingestion.legislative_events import (
    EVENT_VOCABULARY, LegislativeEvent, doc_status_events, item_key, item_kind,
    parse_events,
)


# ---------------------------------------------------------------- item_kind
@pytest.mark.parametrize("item_type,item_number,expected", [
    ("resolution", "20-2026", "resolution"),
    ("Resolution", "20-2026", "resolution"),
    ("ordinance", "1-2026", "ordinance"),
    ("Ordinance", "1-2026", "ordinance"),
    ("Ordinance for Final Passage", "3-2026", "ordinance"),
    ("Ordinance for Amendment", "3-2026", "ordinance"),
    ("", "Bill 3-2026", "ordinance"),          # number prefix rescues empty type
    ("Committee Report", "Administration", None),
    ("Minutes Approval", "", None),
    ("Approval of Minutes", "", None),
    ("Minutes", "", None),
    ("New Business", "", None),
    (None, None, None),
])
def test_item_kind(item_type, item_number, expected):
    assert item_kind(item_type, item_number) == expected


# ----------------------------------------------------------------- item_key
@pytest.mark.parametrize("raw,expected", [
    ("Bill 3-2026", "3-2026"),
    ("bill 3-2026", "3-2026"),
    ("3-2026", "3-2026"),
    ("Res. 20-2026", "20-2026"),
    ("Resolution No. 20-2026", "20-2026"),
    ("No. 7-2026", "7-2026"),
    ("#7-2026", "7-2026"),
    ("  1-2026  ", "1-2026"),
    ("", ""),
    (None, ""),
])
def test_item_key(raw, expected):
    assert item_key(raw) == expected


def test_item_key_identity_across_prefix_variants():
    assert item_key("Bill 3-2026") == item_key("3-2026")


# -------------------------------------------------------------- parse_events
def _names(events):
    return [e.event for e in events]


@pytest.mark.parametrize("action,expected", [
    ("read into record", ["introduced"]),
    ("Read into record for amendment", ["introduced"]),
    ("first reading", ["first_reading"]),
    ("referred to committee", ["referred_to_committee"]),
    ("Referred to Administration Committee", ["referred_to_committee"]),
    ("Moved to committee", ["referred_to_committee"]),
    ("Moved into Budget & Finance Committee", ["referred_to_committee"]),
    ("Reported out", ["reported_out"]),
    ("final passage", ["passed"]),
    ("Passed 7-0", ["passed"]),
    ("approved", ["passed"]),
    ("tabled", ["tabled"]),
    ("withdrawn", ["withdrawn"]),
    ("defeated", ["defeated"]),
    ("motion failed", ["defeated"]),
    ("amendment adopted 7-0", ["amended"]),
    ("amendment passed 6-0", ["amended"]),
])
def test_single_clause_events(action, expected):
    assert _names(parse_events("resolution", action)) == expected


def test_multi_clause_splits_semicolon():
    evs = parse_events("ordinance", "first reading; referred to committee")
    assert _names(evs) == ["first_reading", "referred_to_committee"]


def test_multi_clause_splits_em_dash():
    evs = parse_events("Ordinance", "First Reading — referred to Administration Committee")
    assert _names(evs) == ["first_reading", "referred_to_committee"]
    assert evs[1].committee == "Administration Committee"


def test_committee_extracted_from_referral():
    evs = parse_events("Resolution", "Referred to Parks, Recreation & Enrichment Committee")
    assert evs[0].committee == "Parks, Recreation & Enrichment Committee"


def test_tally_parsed():
    evs = parse_events("Resolution", "Passed 6-1, with Councilwoman Davis voting in the negative")
    assert _names(evs) == ["passed"]
    assert (evs[0].ayes, evs[0].nays) == (6, 1)
    assert "voting in the negative" in evs[0].note


def test_dissent_clause_is_not_defeated():
    # "voting in the negative" must never read as a defeat when ayes > nays
    evs = parse_events(
        "Resolution",
        "Passed 4-3; Vice President Jones, Councilwoman Rawls, and President Hill voted in the negative",
    )
    assert _names(evs) == ["passed"]
    assert (evs[0].ayes, evs[0].nays) == (4, 3)


def test_did_not_pass_is_defeated():
    assert _names(parse_events("resolution", "motion did not pass")) == ["defeated"]


def test_passed_as_amended_emits_both():
    evs = parse_events("Ordinance for Final Passage", "Passed 7-0 as amended")
    assert _names(evs) == ["passed", "amended"]
    assert (evs[0].ayes, evs[0].nays) == (7, 0)


def test_recusal_clause_becomes_note_not_flag():
    evs = parse_events(
        "Ordinance for Final Passage",
        "Passed 6-0; Councilwoman Green recused herself due to potential conflict of interest",
    )
    assert _names(evs) == ["passed"]
    assert "recused" in evs[0].note


def test_final_passage_with_extra_reading_clause():
    evs = parse_events("Ordinance", "Passed 7-0 (Final Passage); also read for Amendment")
    assert evs[0].event == "passed"


def test_unmapped_string_returns_empty():
    assert parse_events("Resolution", "Announced for March 31, 2026 at 5:30 PM") == []


@pytest.mark.parametrize("action", [None, "", "None"])
def test_null_like_actions_yield_nothing(action):
    assert parse_events("Ordinance", action) == []


def test_long_amendment_narrative_keeps_note():
    action = ("Read into record for amendment; amendment added language to subsection "
              "2-301.5.C prohibiting Mayor from appointing interim or temporary department "
              "head and prohibiting Controller from approving compensation for such "
              "appointment; amendment passed 7-0")
    evs = parse_events("Ordinance for Amendment", action)
    assert _names(evs) == ["introduced", "amended"]
    assert "added language" in evs[0].note      # unmatched clause → note on prior event
    assert (evs[1].ayes, evs[1].nays) == (7, 0)


def test_all_events_in_vocabulary():
    samples = [
        "read into record", "first reading", "referred to committee", "Reported out",
        "Passed 7-0", "tabled", "withdrawn", "defeated", "amendment adopted 7-0",
    ]
    for s in samples:
        for e in parse_events("resolution", s):
            assert e.event in EVENT_VOCABULARY


# ---------------------------------------------------------- doc_status_events
@pytest.mark.parametrize("status,expected", [
    ("Passed", ["passed"]),
    ("Adopted", ["passed"]),
    ("Approved", ["passed"]),
    ("Introduced", ["introduced"]),
    ("Signed", ["signed"]),
    ("Vetoed", ["vetoed"]),
    ("veto_overridden", ["veto_overridden"]),
    ("Veto Overridden", ["veto_overridden"]),
    ("Amended", ["amended"]),
    ("", []),
    (None, []),
])
def test_doc_status_events(status, expected):
    assert doc_status_events(status) == expected
