from pathlib import Path

from src.ingestion import pipeline as P
from src.models import ParsedDocument, ParsedElement


def _pipe():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    from src.config import get_settings
    pipe.cfg = get_settings()
    return pipe


def _doc(parser, text):
    el = ParsedElement(text=text, element_type="NarrativeText", page_number=1)
    return ParsedDocument(source_file="f.pdf", parser_used=parser, elements=[el], total_pages=1)


_GIB = "Yy Aavos AouoH feuoneN yd eddey nig No AYaId0S ssauisng euisis Bute eleg"
_GOOD = ("WHEREAS the City of Harrisburg City Council shall authorize the Mayor to "
         "enter into the agreement and be it resolved by the council of the city.")


def test_garbled_clean_text_escalates_to_vision(monkeypatch):
    # clean_text path returns gibberish -> is_garbled -> re-read with Vision
    monkeypatch.setattr(P.unstructured_parser, "parse", lambda p: _doc("unstructured", _GIB))
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: _doc("vision_llm", _GOOD))
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "clean_text_pdf")
    assert out.parser_used == "vision_llm"


def test_readable_clean_text_stays(monkeypatch):
    monkeypatch.setattr(P.unstructured_parser, "parse", lambda p: _doc("unstructured", _GOOD))
    monkeypatch.setattr(P.vision_parser, "parse",
                        lambda p, c: (_ for _ in ()).throw(AssertionError("no vision")))
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "clean_text_pdf")
    assert out.parser_used == "unstructured"
