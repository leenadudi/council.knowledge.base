"""
Hybrid golden-set test harness — exact hard facts + LLM judge.

Structure
---------
* CASES  : glob of all tests/fixtures/**/*.expected.json
           With zero fixtures present, CASES is empty and the parametrized
           test is simply never collected (pytest skips it cleanly).
* @pytest.mark.integration  : requires ANTHROPIC_API_KEY + real fixture .txt files.
* Non-integration unit tests for the pure _dig helper live at the bottom.

Adding a fixture
----------------
See tests/fixtures/README.md.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime

import pytest

from src.models import (
    Chunk,
    ChunkMetadata,
    ParsedDocument,
    ParsedElement,
)


# ---------------------------------------------------------------------------
# Helper: path-based accessor for nested dicts / lists
# ---------------------------------------------------------------------------

def _dig(obj, path: str):
    """
    Navigate a nested dict/list using a dot-and-bracket path.

    Examples
    --------
    _dig({"resolutions": [{"amount": 40000.0}]}, "resolutions[0].amount")
    -> 40000.0

    _dig({"votes": [{"vote": "yes"}, {"vote": "no"}]}, "votes[1].vote")
    -> "no"

    _dig({"a": {"b": 1}}, "a.b")
    -> 1
    """
    # Tokenise: split on "." but keep "[N]" suffixes attached to their key
    # e.g. "resolutions[0].amount" → ["resolutions[0]", "amount"]
    tokens = re.split(r"\.", path)
    current = obj
    for token in tokens:
        try:
            # Check for array index: key[N]
            m = re.fullmatch(r"(\w+)\[(\d+)\]", token)
            if m:
                key, idx = m.group(1), int(m.group(2))
                current = current[key][idx]
            else:
                current = current[token]
        except (KeyError, IndexError, TypeError) as e:
            raise KeyError(f"_dig failed for path {path!r} at token {token!r}: {e}") from e
    return current


# ---------------------------------------------------------------------------
# Helpers: build ParsedDocument and Chunk list from raw text
# ---------------------------------------------------------------------------

def _parsed_from_text(text: str, source_file: str = "fixture.txt") -> ParsedDocument:
    """Wrap plain text in a single-page ParsedDocument (one NarrativeText element)."""
    return ParsedDocument(
        source_file=source_file,
        parser_used="unstructured",
        elements=[ParsedElement(element_type="NarrativeText", text=text, page_number=1)],
        total_pages=1,
    )


def _chunks_from_text(
    text: str,
    source_file: str = "fixture.txt",
    document_type: str = "resolution",
    department: str = "City Council",
) -> list[Chunk]:
    """Return a single Chunk wrapping the entire text (sufficient for the judge harness)."""
    meta = ChunkMetadata(
        source_file=source_file,
        department=department,
        document_type=document_type,
        section="body",
        content_type="narrative",
        page_number=1,
        parser_used="unstructured",
        ingestion_timestamp=datetime.utcnow().isoformat(),
        chunk_index=0,
        total_chunks_in_doc=1,
    )
    return [Chunk(text=text, metadata=meta)]


# ---------------------------------------------------------------------------
# Golden-set parametrized test (integration — requires API key + fixture docs)
# ---------------------------------------------------------------------------

# Resolve relative to this file's location so the test works from any cwd.
_FIXTURE_GLOB = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "**", "*.expected.json"
)
CASES = glob.glob(_FIXTURE_GLOB, recursive=True)


@pytest.mark.integration
@pytest.mark.parametrize("expected_path", CASES)
def test_golden_case(expected_path):
    """
    For each *.expected.json fixture:
    1. Classify the document (profile) and assert document_type matches.
    2. Extract via extract_for_type and assert every hard_fact path exactly.
    3. Judge extraction quality and assert score >= 4, no hallucination.
    """
    from src.ingestion.profiler import profile_document
    from src.ingestion.registry import get_document_type
    from src.extraction.sql_extractor import SQLExtractor
    from src.evaluation.ingestion_judge import judge_extraction

    spec = json.load(open(expected_path, encoding="utf-8"))
    src_dir = os.path.dirname(expected_path)
    source_text = open(os.path.join(src_dir, spec["source_text_file"]), encoding="utf-8").read()

    # --- 1. Profile / classify -----------------------------------------------
    parsed = _parsed_from_text(source_text, source_file=spec["source_text_file"])
    profile = profile_document(parsed, spec["source_text_file"])
    assert profile.document_type == spec["document_type"], (
        f"Expected document_type={spec['document_type']!r}, "
        f"got {profile.document_type!r} (confidence={profile.confidence})"
    )

    # --- 2. Hard-fact extraction ----------------------------------------------
    doc_type = get_document_type(profile.document_type)
    assert doc_type is not None, f"Unknown document type: {profile.document_type!r}"

    chunks = _chunks_from_text(
        source_text,
        source_file=spec["source_text_file"],
        document_type=profile.document_type,
    )
    extracted = SQLExtractor().extract_for_type(chunks, doc_type)

    for path, want in spec.get("hard_facts", {}).items():
        got = _dig(extracted, path)
        assert got == want, f"hard_fact {path!r}: got {got!r}, want {want!r}"

    # --- 3. LLM judge (soft) --------------------------------------------------
    verdict = judge_extraction(
        source_text=source_text,
        extracted=extracted,
        expected_notes=spec.get("judge_notes", ""),
    )
    assert verdict["score"] >= 4, (
        f"Judge score too low: {verdict['score']} — {verdict['reasoning']}"
    )
    assert not verdict["hallucinated"], (
        f"Judge detected hallucination: {verdict['reasoning']}"
    )


# ---------------------------------------------------------------------------
# Unit tests for _dig (no API key, no integration mark)
# ---------------------------------------------------------------------------

def test_dig_simple_key():
    assert _dig({"amount": 40000.0}, "amount") == 40000.0


def test_dig_nested_key():
    assert _dig({"a": {"b": {"c": 99}}}, "a.b.c") == 99


def test_dig_array_index():
    obj = {"resolutions": [{"amount": 40000.0}]}
    assert _dig(obj, "resolutions[0].amount") == 40000.0


def test_dig_array_index_second_element():
    obj = {"votes": [{"vote": "yes"}, {"vote": "no"}]}
    assert _dig(obj, "votes[1].vote") == "no"


def test_dig_string_value():
    obj = {"resolutions": [{"resolution_number": "2026-R-12", "amount": 40000.0}]}
    assert _dig(obj, "resolutions[0].resolution_number") == "2026-R-12"


def test_dig_date_value():
    obj = {"resolutions": [{"adopted_date": "2026-03-03"}]}
    assert _dig(obj, "resolutions[0].adopted_date") == "2026-03-03"


def test_dig_missing_key_raises_with_context():
    """A missing path should raise KeyError with the path name in the message."""
    obj = {"resolutions": [{"amount": 40000.0}]}
    with pytest.raises(KeyError, match="resolutions"):
        _dig(obj, "resolutions[0].missing_key")
