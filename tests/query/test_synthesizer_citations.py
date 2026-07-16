"""Citations must name the source PDF the user recognizes — never the datastore."""

from src.config import Settings
from src.models import RetrievalResult
from src.query.synthesizer import Synthesizer


class _DummyLLM:  # never called by _format_context
    pass


def _synth():
    return Synthesizer(Settings(anthropic_api_key="x"), llm=_DummyLLM())


def test_sql_answer_cites_pdf_not_database():
    s = _synth()
    res = [RetrievalResult(store="sql", sql_rows=[
        {"resolution_number": "1-2026", "amount": 5000, "source_file": "Resolution 1-2026.pdf"},
        {"resolution_number": "9-2026", "amount": 8000, "source_file": "Resolution 9-2026.pdf"},
    ])]
    ctx, cites = s._format_context(res)
    # The old DB label must be gone…
    assert "SQL Database Results" not in ctx
    # …replaced by the actual source documents.
    assert "Resolution 1-2026.pdf" in ctx
    assert {c.source_file for c in cites} == {"Resolution 1-2026.pdf", "Resolution 9-2026.pdf"}


def test_sql_sources_deduped_and_ordered():
    s = _synth()
    res = [RetrievalResult(store="sql", sql_rows=[
        {"x": 1, "source_file": "A.pdf"},
        {"x": 2, "source_file": "A.pdf"},
        {"x": 3, "source_file": "B.pdf"},
    ])]
    _, cites = s._format_context(res)
    assert [c.source_file for c in cites] == ["A.pdf", "B.pdf"]


def test_graph_answer_never_labels_database():
    s = _synth()
    res = [RetrievalResult(store="graph", graph_data={"records": [
        {"p.name": "Jane Doe", "p.title": "Director"},
    ]})]
    ctx, _ = s._format_context(res)
    assert "Graph Database Results" not in ctx


def test_graph_pdf_values_become_citations():
    s = _synth()
    res = [RetrievalResult(store="graph", graph_data={"records": [
        {"p.name": "Jane Doe", "doc.filename": "Org Chart 2026.pdf"},
    ]})]
    _, cites = s._format_context(res)
    assert any(c.source_file == "Org Chart 2026.pdf" for c in cites)


def test_vector_citations_unchanged():
    s = _synth()
    res = [RetrievalResult(store="vector", chunks=[
        {"payload": {"text": "hello", "source_file": "Q1 Report.pdf", "section": "Budget"},
         "chunk_id": "c1"},
    ])]
    ctx, cites = s._format_context(res)
    assert "[Source: Q1 Report.pdf" in ctx
    assert cites[0].source_file == "Q1 Report.pdf" and cites[0].section == "Budget"
