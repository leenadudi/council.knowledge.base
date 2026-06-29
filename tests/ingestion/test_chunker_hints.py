from src.ingestion.chunker import chunk_document
from src.models import ParsedDocument, ParsedElement, ChunkingHints


def _doc(elems):
    return ParsedDocument(source_file="r.pdf", parser_used="unstructured",
                          elements=elems, total_pages=1)


def test_keep_together_does_not_split_whereas_resolved():
    big_whereas = "WHEREAS " + ("the city finds it necessary " * 200)
    resolved = "RESOLVED that the contract is authorized."
    doc = _doc([ParsedElement("NarrativeText", big_whereas, 1),
                ParsedElement("NarrativeText", resolved, 1)])
    hints = ChunkingHints(keep_together=["whereas", "resolved"])
    chunks = chunk_document(doc, hints=hints)
    # the RESOLVED conclusion must live in the same chunk as its WHEREAS reasoning
    joined = [c for c in chunks if "RESOLVED that the contract" in c["text"]]
    assert joined and "WHEREAS" in joined[0]["text"]


def test_default_behavior_unchanged_without_hints():
    # Table text must exceed min_chunk_size (100) to survive the flush gate
    table_text = "Account 100 ... 5000.00  " + ("row data  " * 10)
    doc = _doc([ParsedElement("Title", "Budget", 1),
                ParsedElement("Table", table_text, 1)])
    chunks = chunk_document(doc)  # no hints → existing path
    assert any(c["element_type"] == "Table" for c in chunks)
