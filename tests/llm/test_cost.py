from types import SimpleNamespace

from src.llm.client import PRICES, UsageRecord, estimate_cost


def test_estimate_cost_sonnet_input_output():
    # 1,000,000 input @ $3 + 1,000,000 output @ $15 = $18.00
    assert estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000, 0, 0) == 18.0


def test_estimate_cost_counts_cache_tokens():
    # 1M cache-read @ $0.30 + 1M cache-write @ $3.75 = $4.05
    assert estimate_cost("claude-sonnet-4-6", 0, 0, 1_000_000, 1_000_000) == 4.05


def test_estimate_cost_haiku_cheaper_than_sonnet():
    h = estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000, 0, 0)
    s = estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000, 0, 0)
    assert h == 6.0 and h < s


def test_estimate_cost_unknown_model_falls_back_to_sonnet():
    assert estimate_cost("some-future-model", 1_000_000, 0, 0, 0) == 3.0


def test_usage_record_from_response_reads_usage_and_costs():
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=200,
        cache_creation_input_tokens=100,
    )
    response = SimpleNamespace(usage=usage)
    rec = UsageRecord.from_response(
        call_site="query.synthesizer",
        model="claude-sonnet-4-6",
        response=response,
        latency_ms=1234,
        query_id="11111111-1111-1111-1111-111111111111",
    )
    assert rec.call_site == "query.synthesizer"
    assert rec.input_tokens == 1000 and rec.output_tokens == 500
    assert rec.cache_read_tokens == 200 and rec.cache_write_tokens == 100
    assert rec.latency_ms == 1234
    # (1000*3 + 500*15 + 200*0.30 + 100*3.75) / 1e6
    assert rec.est_cost_usd == round((1000 * 3 + 500 * 15 + 200 * 0.30 + 100 * 3.75) / 1_000_000, 6)


def test_usage_record_from_response_handles_missing_cache_fields():
    usage = SimpleNamespace(input_tokens=10, output_tokens=20)  # no cache fields
    rec = UsageRecord.from_response(
        call_site="ingestion.classifier", model="claude-sonnet-4-6",
        response=SimpleNamespace(usage=usage), latency_ms=5,
    )
    assert rec.cache_read_tokens == 0 and rec.cache_write_tokens == 0


def test_usage_record_as_dict_shapes_uuid_and_keys():
    rec = UsageRecord(
        call_site="x", model="claude-sonnet-4-6", input_tokens=1, output_tokens=2,
        cache_read_tokens=0, cache_write_tokens=0, est_cost_usd=0.1, latency_ms=3,
        query_id="22222222-2222-2222-2222-222222222222",
    )
    d = rec.as_dict()
    import uuid
    assert isinstance(d["id"], uuid.UUID)
    assert isinstance(d["query_id"], uuid.UUID)
    assert d["batch_id"] is None
    assert set(d) == {
        "id", "call_site", "model", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "est_cost_usd", "latency_ms",
        "query_id", "batch_id",
    }


def test_prices_table_has_known_models():
    assert "claude-sonnet-4-6" in PRICES and "claude-haiku-4-5" in PRICES
