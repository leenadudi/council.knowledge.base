import types
from src.ingestion.parsers import tesseract_parser as tp
from src.models import ParsedDocument, ParsedElement


def test_split_blocks_on_blank_lines():
    text = "WHEREAS the city finds it necessary\n\nRESOLVED that it is authorized\n\n  \n"
    assert tp._split_blocks(text) == ["WHEREAS the city finds it necessary", "RESOLVED that it is authorized"]


def test_parse_builds_tesseract_elements(monkeypatch):
    # Fake 2 rendered pages; OCR returns known text per page.
    monkeypatch.setattr(tp.pdf2image, "convert_from_path", lambda *a, **k: ["img1", "img2"])
    pages = {"img1": "RESOLUTION NO. 9-2026\n\nWHEREAS the city ...", "img2": "YEAS\n\nMS. DAVIS"}
    monkeypatch.setattr(tp, "_ocr_image", lambda im: pages[im])
    doc = tp.parse("whatever.pdf")
    assert doc.parser_used == "tesseract"
    assert doc.total_pages == 2
    # blocks split; markers land at element starts
    texts = [e.text for e in doc.elements]
    assert texts[0].startswith("RESOLUTION NO. 9-2026")
    assert any(t.startswith("WHEREAS") for t in texts)
    assert any(t.startswith("YEAS") for t in texts)
    assert doc.elements[-1].page_number == 2


def test_parse_page_ocr_error_is_isolated(monkeypatch):
    monkeypatch.setattr(tp.pdf2image, "convert_from_path", lambda *a, **k: ["p1"])
    def boom(im): raise RuntimeError("tess crash")
    monkeypatch.setattr(tp, "_ocr_image", boom)
    doc = tp.parse("x.pdf")
    assert doc.parser_used == "tesseract" and len(doc.elements) == 1
    assert "OCR failed" in doc.elements[0].text   # placeholder, no raise


def _doc(text, pages):
    return ParsedDocument(source_file="x", parser_used="tesseract",
                          elements=[ParsedElement("NarrativeText", text, 1)], total_pages=pages)


def test_ocr_quality_ok_dense_text_true():
    assert tp.ocr_quality_ok(_doc("A" * 400, pages=1)) is True


def test_ocr_quality_ok_sparse_text_false():
    assert tp.ocr_quality_ok(_doc("short", pages=1)) is False    # < 150 chars/page
