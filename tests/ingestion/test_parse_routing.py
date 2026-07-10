# tests/ingestion/test_parse_routing.py
from pathlib import Path
from src.ingestion import pipeline as P
from src.models import ParsedDocument, ParsedElement


def _pipe():
    pipe = P.IngestionPipeline.__new__(P.IngestionPipeline)
    from src.config import get_settings
    pipe.cfg = get_settings()
    return pipe


# A quality-OK parse carries readable text (so it does not trip the garble escalation).
_READABLE = "The City Council of the City of Harrisburg shall meet and be it resolved."


def _tess_doc():
    el = ParsedElement(text=_READABLE, element_type="NarrativeText", page_number=1)
    return ParsedDocument("f.pdf", "tesseract", [el], 3)


def _vis_doc():  return ParsedDocument("f.pdf", "vision_llm", [], 3)


def test_complex_pdf_uses_tesseract_when_quality_ok(monkeypatch):
    monkeypatch.setattr(P.tesseract_parser, "parse", lambda p, c: _tess_doc())
    monkeypatch.setattr(P.tesseract_parser, "ocr_quality_ok", lambda d, c: True)
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: (_ for _ in ()).throw(AssertionError("vision should not run")))
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "complex_pdf")
    assert out.parser_used == "tesseract"


def test_complex_pdf_falls_back_to_vision_when_quality_poor(monkeypatch):
    monkeypatch.setattr(P.tesseract_parser, "parse", lambda p, c: _tess_doc())
    monkeypatch.setattr(P.tesseract_parser, "ocr_quality_ok", lambda d, c: False)
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: _vis_doc())
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "complex_pdf")
    assert out.parser_used == "vision_llm"


def test_complex_pdf_falls_back_to_vision_when_tesseract_raises(monkeypatch):
    def boom(p, c): raise RuntimeError("tess down")
    monkeypatch.setattr(P.tesseract_parser, "parse", boom)
    monkeypatch.setattr(P.vision_parser, "parse", lambda p, c: _vis_doc())
    out = _pipe()._parse_with_fallback(Path("f.pdf"), "complex_pdf")
    assert out.parser_used == "vision_llm"
