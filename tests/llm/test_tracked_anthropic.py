from types import SimpleNamespace

import pytest

from src.config import Settings
from src.llm.client import TrackedAnthropic, UsageRecord


class FakeMessages:
    def __init__(self, usage):
        self._usage = usage
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(usage=self._usage, content=[SimpleNamespace(text="ok")])


class FakeClient:
    def __init__(self, usage):
        self.messages = FakeMessages(usage)


def _usage(inp=100, out=50):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )


def _tracked(sink, fake):
    return TrackedAnthropic(
        Settings(anthropic_api_key="x"),
        call_site="query.synthesizer",
        client=fake,
        sink=sink,
    )


def test_create_returns_underlying_response_and_passes_kwargs():
    fake = FakeClient(_usage())
    recorded = []
    t = _tracked(recorded.append, fake)
    resp = t.messages.create(model="claude-sonnet-4-6", max_tokens=10,
                             messages=[{"role": "user", "content": "hi"}])
    assert resp.content[0].text == "ok"
    # query_id/batch_id are stripped before reaching the SDK
    assert fake.messages.calls[0] == {
        "model": "claude-sonnet-4-6", "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_create_emits_one_record_with_call_site_and_query_id():
    fake = FakeClient(_usage(inp=200, out=40))
    recorded = []
    t = _tracked(recorded.append, fake)
    t.messages.create(model="claude-sonnet-4-6", max_tokens=10,
                      messages=[{"role": "user", "content": "hi"}],
                      query_id="33333333-3333-3333-3333-333333333333")
    assert len(recorded) == 1
    rec = recorded[0]
    assert isinstance(rec, UsageRecord)
    assert rec.call_site == "query.synthesizer"
    assert rec.model == "claude-sonnet-4-6"
    assert rec.input_tokens == 200 and rec.output_tokens == 40
    assert rec.query_id == "33333333-3333-3333-3333-333333333333"
    assert rec.latency_ms >= 0


def test_sink_failure_does_not_break_the_call():
    fake = FakeClient(_usage())

    def boom(_record):
        raise RuntimeError("db down")

    t = _tracked(boom, fake)
    resp = t.messages.create(model="claude-sonnet-4-6", max_tokens=10,
                             messages=[{"role": "user", "content": "hi"}])
    assert resp.content[0].text == "ok"  # call still succeeds


def test_default_sink_is_noop_when_not_provided():
    fake = FakeClient(_usage())
    t = TrackedAnthropic(Settings(anthropic_api_key="x"),
                         call_site="ingestion.classifier", client=fake)
    # No sink provided -> no exception, call returns normally.
    resp = t.messages.create(model="claude-sonnet-4-6", max_tokens=10,
                             messages=[{"role": "user", "content": "hi"}])
    assert resp.content[0].text == "ok"
