from src.config import Settings
from src.llm.client import UsageRecord, pg_usage_sink


class FakeStore:
    instances = []

    def __init__(self, cfg):
        self.cfg = cfg
        self.inserted = []
        self.closed = False
        FakeStore.instances.append(self)

    def insert_llm_usage(self, record):
        self.inserted.append(record)

    def close(self):
        self.closed = True


def _record():
    return UsageRecord(
        call_site="query.synthesizer", model="claude-sonnet-4-6",
        input_tokens=1, output_tokens=2, cache_read_tokens=0, cache_write_tokens=0,
        est_cost_usd=0.1, latency_ms=3,
        query_id="44444444-4444-4444-4444-444444444444",
    )


def test_pg_sink_inserts_record_dict_and_closes(monkeypatch):
    FakeStore.instances = []
    monkeypatch.setattr("src.llm.client.SQLStore", FakeStore)
    sink = pg_usage_sink(Settings(anthropic_api_key="x"))
    sink(_record())
    assert len(FakeStore.instances) == 1
    store = FakeStore.instances[0]
    assert len(store.inserted) == 1
    assert store.inserted[0]["call_site"] == "query.synthesizer"
    assert store.closed is True


def test_pg_sink_closes_even_if_insert_fails(monkeypatch):
    class BoomStore(FakeStore):
        def insert_llm_usage(self, record):
            raise RuntimeError("insert failed")

    FakeStore.instances = []
    monkeypatch.setattr("src.llm.client.SQLStore", BoomStore)
    sink = pg_usage_sink(Settings(anthropic_api_key="x"))
    # The sink itself may raise; TrackedAnthropic._create swallows it. Here we
    # only require that the store was closed before the exception propagated.
    try:
        sink(_record())
    except RuntimeError:
        pass
    assert FakeStore.instances[0].closed is True
