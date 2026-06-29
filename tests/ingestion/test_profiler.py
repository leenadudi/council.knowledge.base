import json
from src.ingestion.profiler import profile_document
from src.models import ParsedDocument, ParsedElement


class _FakeMsg:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    class _M:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return _FakeMsg(self._outer._payload)

    @property
    def messages(self):
        return _FakeClient._M(self)


def _parsed(text):
    return ParsedDocument(
        source_file="x.pdf",
        parser_used="unstructured",
        elements=[ParsedElement("NarrativeText", text, 1)],
        total_pages=1,
    )


def test_profiler_returns_known_type():
    payload = json.dumps({
        "document_type": "resolution",
        "department": "DEDBH",
        "period": "2026-03-03",
        "title": "RES 2026-R-12",
        "identifying_ids": {"resolution_number": "2026-R-12"},
        "confidence": 0.92,
    })
    p = profile_document(
        _parsed("RESOLUTION NO 2026-R-12 ... WHEREAS ... RESOLVED"),
        "res12.pdf",
        client=_FakeClient(payload),
    )
    assert p.document_type == "resolution"
    assert p.confidence == 0.92


def test_profiler_uses_profiler_model_and_first_pages():
    payload = json.dumps({
        "document_type": "quarterly_report",
        "department": "Health Office",
        "period": "Q1 2026",
        "title": "",
        "identifying_ids": {},
        "confidence": 0.8,
    })
    c = _FakeClient(payload)
    profile_document(_parsed("Quarterly Report Health Office"), "h.pdf", client=c)
    assert c.calls[0]["model"].startswith("claude-haiku")
