# LLM Cost Instrumentation (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every Anthropic call through one wrapper that records per-call token usage and estimated cost to a `llm_usage` Postgres table, and expose a cost-by-call-site report — without changing any model, prompt, or output.

**Architecture:** A `TrackedAnthropic` wrapper (in a new `src/llm/` module) exposes the same `messages.create(...)` surface as the SDK, times the call, reads `response.usage`, computes cost from a per-model price table, and writes a best-effort, non-blocking row via a pluggable sink (default: a fresh short-lived Postgres connection per write — thread-safe, no shared state). The 7 existing call sites swap their local `anthropic.Anthropic(...)` construction for `TrackedAnthropic(...)` with a `call_site` label; query-path sites also pass `query_id`. A `/admin/costs` route and `SQLStore.usage_report()` aggregate the table.

**Tech Stack:** Python 3.14, `anthropic` SDK, `psycopg2`, `pydantic-settings`, Flask, `pytest`.

## Global Constraints

- **Behavior-preserving:** Phase 1 changes no model, prompt, parameter, or output. Every refactored call site uses the same `model=`, `max_tokens=`, and `messages=` as before.
- **Recording is best-effort and never blocks or breaks a call:** all usage recording is wrapped so any failure (DB down, bad row) is logged and swallowed; the underlying API response is always returned.
- **Models unchanged:** all call sites keep `claude-sonnet-4-6` (synthesis/`vision_model`) exactly as today. Model right-sizing is Phase 3, not this plan.
- **Match existing patterns:** new SQL goes in `sql/schema.sql`; new DB methods go on `SQLStore` and use its `self.cursor()` context manager; `uuid.UUID` for UUID columns (the repo calls `psycopg2.extras.register_uuid()`).
- **Test runner:** `python3 -m pytest` from the repo root.

---

### Task 1: Pricing table, cost estimator, and `UsageRecord`

Pure, offline, no DB or network. Also bootstraps the test harness.

**Files:**
- Create: `src/llm/__init__.py` (empty)
- Create: `src/llm/client.py` (pricing + estimator + record only; wrapper added in Task 2)
- Create: `tests/__init__.py` (empty), `tests/llm/__init__.py` (empty)
- Create: `tests/llm/test_cost.py`
- Create: `pytest.ini`
- Create: `requirements-dev.txt`

**Interfaces:**
- Produces:
  - `PRICES: dict[str, tuple[float, float, float, float]]` — model → (input, output, cache_read, cache_write) USD per 1M tokens.
  - `estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int, cache_write_tokens: int) -> float`
  - `@dataclass UsageRecord` with fields `call_site, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, est_cost_usd, latency_ms, query_id: Optional[str]=None, batch_id: Optional[str]=None`; classmethod `from_response(*, call_site, model, response, latency_ms, query_id=None, batch_id=None) -> UsageRecord`; method `as_dict() -> dict` (generates a fresh `id` UUID and converts `query_id` to `uuid.UUID`).

- [ ] **Step 1: Create the pytest config**

Create `pytest.ini`:

```ini
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 2: Create the dev requirements file**

Create `requirements-dev.txt`:

```
pytest>=8.0.0
```

- [ ] **Step 3: Create empty package markers**

Create `src/llm/__init__.py`, `tests/__init__.py`, and `tests/llm/__init__.py` each as empty files.

- [ ] **Step 4: Write the failing test**

Create `tests/llm/test_cost.py`:

```python
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
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `python3 -m pytest tests/llm/test_cost.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.llm.client'` (or `ImportError`).

- [ ] **Step 6: Write the implementation**

Create `src/llm/client.py`:

```python
"""
TrackedAnthropic — a thin wrapper around the Anthropic SDK that records
per-call token usage and estimated cost. Pricing, the cost estimator, and the
UsageRecord live here; the wrapper class is added in a later task.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

# USD per 1,000,000 tokens: (input, output, cache_read, cache_write).
# cache_read ~= 0.1x input, cache_write ~= 1.25x input.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}
_DEFAULT_PRICE = PRICES["claude-sonnet-4-6"]


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    inp, out, cr, cw = PRICES.get(model, _DEFAULT_PRICE)
    total = (
        input_tokens * inp
        + output_tokens * out
        + cache_read_tokens * cr
        + cache_write_tokens * cw
    )
    return round(total / 1_000_000, 6)


@dataclass
class UsageRecord:
    call_site: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    est_cost_usd: float
    latency_ms: int
    query_id: Optional[str] = None
    batch_id: Optional[str] = None

    @classmethod
    def from_response(
        cls,
        *,
        call_site: str,
        model: str,
        response: Any,
        latency_ms: int,
        query_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> "UsageRecord":
        u = getattr(response, "usage", None)
        input_tokens = int(getattr(u, "input_tokens", 0) or 0)
        output_tokens = int(getattr(u, "output_tokens", 0) or 0)
        cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        return cls(
            call_site=call_site,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            est_cost_usd=estimate_cost(
                model, input_tokens, output_tokens, cache_read, cache_write
            ),
            latency_ms=latency_ms,
            query_id=query_id,
            batch_id=batch_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": uuid.uuid4(),
            "call_site": self.call_site,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "est_cost_usd": self.est_cost_usd,
            "latency_ms": self.latency_ms,
            "query_id": uuid.UUID(self.query_id) if self.query_id else None,
            "batch_id": self.batch_id,
        }
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `python3 -m pytest tests/llm/test_cost.py -v`
Expected: PASS (8 passed).

- [ ] **Step 8: Commit**

```bash
git add pytest.ini requirements-dev.txt src/llm/__init__.py src/llm/client.py tests/__init__.py tests/llm/__init__.py tests/llm/test_cost.py
git commit -m "feat(llm): add price table, cost estimator, and UsageRecord"
```

---

### Task 2: `TrackedAnthropic` wrapper

Wraps the SDK, times the call, builds a `UsageRecord`, and emits it to a sink. Default sink is a no-op here (Task 3 swaps the default to Postgres). All recording is wrapped so failures never break the call.

**Files:**
- Modify: `src/llm/client.py` (append wrapper + `_noop_sink`)
- Create: `tests/llm/test_tracked_anthropic.py`

**Interfaces:**
- Consumes: `UsageRecord` (Task 1).
- Produces:
  - `_noop_sink(record: UsageRecord) -> None`
  - `class TrackedAnthropic` with `__init__(self, settings=None, *, call_site: str, client=None, sink=None)`, attribute `call_site`, attribute `messages` (an object exposing `create(*, query_id=None, batch_id=None, **kwargs)`), and `_create(...)`. When `client` is None it constructs `anthropic.Anthropic(api_key=settings.anthropic_api_key)`; when `sink` is None it uses `_noop_sink` (Task 3 changes this default).

- [ ] **Step 1: Write the failing test**

Create `tests/llm/test_tracked_anthropic.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/llm/test_tracked_anthropic.py -v`
Expected: FAIL with `ImportError: cannot import name 'TrackedAnthropic'`.

- [ ] **Step 3: Append the implementation to `src/llm/client.py`**

Add these imports to the top of `src/llm/client.py` (alongside the existing `import uuid`):

```python
import logging
import time
from typing import Callable

import anthropic

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)
```

Append to the end of `src/llm/client.py`:

```python
def _noop_sink(record: UsageRecord) -> None:
    return None


class _Messages:
    def __init__(self, parent: "TrackedAnthropic") -> None:
        self._parent = parent

    def create(self, *, query_id: Optional[str] = None,
               batch_id: Optional[str] = None, **kwargs: Any):
        return self._parent._create(query_id=query_id, batch_id=batch_id, **kwargs)


class TrackedAnthropic:
    """Drop-in stand-in for anthropic.Anthropic that records usage per call_site."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        call_site: str,
        client: Optional["anthropic.Anthropic"] = None,
        sink: Optional[Callable[[UsageRecord], None]] = None,
    ) -> None:
        self.cfg = settings or get_settings()
        self.call_site = call_site
        self._client = client or anthropic.Anthropic(api_key=self.cfg.anthropic_api_key)
        self._sink = sink or _noop_sink  # Task 3 changes this default to pg_usage_sink
        self.messages = _Messages(self)

    def _create(self, *, query_id: Optional[str] = None,
                batch_id: Optional[str] = None, **kwargs: Any):
        model = kwargs.get("model", "")
        t0 = time.time()
        response = self._client.messages.create(**kwargs)
        latency_ms = int((time.time() - t0) * 1000)
        try:
            record = UsageRecord.from_response(
                call_site=self.call_site, model=model, response=response,
                latency_ms=latency_ms, query_id=query_id, batch_id=batch_id,
            )
            self._sink(record)
        except Exception as e:  # never let recording break a real call
            logger.warning("llm usage recording failed (%s): %s", self.call_site, e)
        return response
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/llm/test_tracked_anthropic.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/llm/client.py tests/llm/test_tracked_anthropic.py
git commit -m "feat(llm): add TrackedAnthropic wrapper with pluggable usage sink"
```

---

### Task 3: `llm_usage` table, `SQLStore.insert_llm_usage`, and the Postgres sink

Adds persistence and flips the wrapper's default sink to Postgres. The sink opens a fresh short-lived `SQLStore` per write (thread-safe — the per-query auto-eval runs in background threads).

**Files:**
- Modify: `sql/schema.sql` (append `llm_usage` table)
- Modify: `src/storage/sql_store.py` (add `insert_llm_usage`)
- Modify: `src/llm/client.py` (add `pg_usage_sink`; change `TrackedAnthropic` default sink)
- Create: `tests/llm/test_sink.py`

**Interfaces:**
- Consumes: `UsageRecord` (Task 1), `SQLStore` (existing).
- Produces:
  - `SQLStore.insert_llm_usage(self, record: dict[str, Any]) -> None` — inserts one row; keys match `UsageRecord.as_dict()`.
  - `pg_usage_sink(cfg: Settings) -> Callable[[UsageRecord], None]` in `src/llm/client.py`.
  - `TrackedAnthropic` default sink is now `pg_usage_sink(self.cfg)`.

- [ ] **Step 1: Add the table to `sql/schema.sql`**

Append to `sql/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS llm_usage (
    id                  UUID PRIMARY KEY,
    timestamp           TIMESTAMP DEFAULT NOW(),
    call_site           VARCHAR(64),
    model               VARCHAR(64),
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    est_cost_usd        DECIMAL(10,6),
    latency_ms          INTEGER,
    query_id            UUID,
    batch_id            VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_timestamp ON llm_usage (timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_usage_call_site ON llm_usage (call_site);
```

- [ ] **Step 2: Apply the table to the running database**

Run (creates the table on the existing dev DB; safe to re-run):

```bash
psql "$(python3 -c 'from src.config import get_settings; print(get_settings().database_url)')" -f sql/schema.sql
```

Expected: `CREATE TABLE` / `CREATE INDEX` (or no error if already present).

- [ ] **Step 3: Write the failing test**

Create `tests/llm/test_sink.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `python3 -m pytest tests/llm/test_sink.py -v`
Expected: FAIL with `ImportError: cannot import name 'pg_usage_sink'`.

- [ ] **Step 5: Add `insert_llm_usage` to `SQLStore`**

In `src/storage/sql_store.py`, add this method to the `SQLStore` class (place it after `log_query`, before `update_query_scores`):

```python
    def insert_llm_usage(self, record: dict[str, Any]) -> None:
        sql = """
            INSERT INTO llm_usage (
                id, call_site, model, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, est_cost_usd, latency_ms,
                query_id, batch_id
            ) VALUES (
                %(id)s, %(call_site)s, %(model)s, %(input_tokens)s, %(output_tokens)s,
                %(cache_read_tokens)s, %(cache_write_tokens)s, %(est_cost_usd)s, %(latency_ms)s,
                %(query_id)s, %(batch_id)s
            )
        """
        with self.cursor() as cur:
            cur.execute(sql, record)
```

- [ ] **Step 6: Add `pg_usage_sink` and flip the default sink in `src/llm/client.py`**

Append to the end of `src/llm/client.py`:

```python
def pg_usage_sink(cfg: Settings) -> Callable[[UsageRecord], None]:
    """A sink that writes each record via a fresh short-lived SQLStore.

    A new connection per write keeps this thread-safe (the per-query auto-eval
    runs in background threads) at the cost of a connection per LLM call, which
    is negligible at instrumentation volume.
    """
    from src.storage.sql_store import SQLStore

    def _sink(record: UsageRecord) -> None:
        store = SQLStore(cfg)
        try:
            store.insert_llm_usage(record.as_dict())
        finally:
            store.close()

    return _sink
```

Then change the default-sink line in `TrackedAnthropic.__init__` from:

```python
        self._sink = sink or _noop_sink  # Task 3 changes this default to pg_usage_sink
```

to:

```python
        self._sink = sink or pg_usage_sink(self.cfg)
```

- [ ] **Step 7: Run the new test plus the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS. The Task 2 test `test_default_sink_is_noop_when_not_provided` still passes because it injects a fake `client` and never triggers a real DB write (the default sink is constructed but `pg_usage_sink` is only *called* on `create`, and that fake usage write would hit the DB — see note). If that test now attempts a DB write, update it to pass `sink=lambda r: None` explicitly and rename to `test_explicit_noop_sink`.

> Implementer note: re-run `tests/llm/test_tracked_anthropic.py` specifically. `test_default_sink_is_noop_when_not_provided` constructs a `TrackedAnthropic` with no sink and calls `create`, which now routes to `pg_usage_sink`. Since recording is wrapped in try/except inside `_create`, a DB failure is swallowed and the test's assertion (`resp.content[0].text == "ok"`) still holds — but to keep the unit test hermetic (no DB dependency), change that test to pass `sink=lambda r: None` and assert the call returns normally. Make that edit as part of this step.

Apply this edit to `tests/llm/test_tracked_anthropic.py` — replace `test_default_sink_is_noop_when_not_provided` with:

```python
def test_explicit_noop_sink_returns_normally():
    fake = FakeClient(_usage())
    t = TrackedAnthropic(Settings(anthropic_api_key="x"),
                         call_site="ingestion.classifier", client=fake,
                         sink=lambda r: None)
    resp = t.messages.create(model="claude-sonnet-4-6", max_tokens=10,
                             messages=[{"role": "user", "content": "hi"}])
    assert resp.content[0].text == "ok"
```

Run again: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add sql/schema.sql src/storage/sql_store.py src/llm/client.py tests/llm/test_sink.py tests/llm/test_tracked_anthropic.py
git commit -m "feat(llm): persist usage to llm_usage table via Postgres sink"
```

---

### Task 4: Route the query-path + eval call sites through `TrackedAnthropic`

Refactors the three sites that run per query and threads `query_id` so query cost can be joined to `query_logs`. Behavior-preserving (same model, prompts, params).

**Files:**
- Modify: `src/query/synthesizer.py` (`Synthesizer.__init__`, `synthesize`)
- Modify: `src/query/classifier.py` (`QueryClassifier.__init__`, `classify`)
- Modify: `src/evaluation/evaluator.py` (`Evaluator.__init__`, `evaluate`)
- Modify: `src/query/pipeline.py` (`QueryPipeline.ask` passes `query_id` to `classify`)
- Create: `tests/query/__init__.py` (empty)
- Create: `tests/query/test_query_path_instrumentation.py`

**Interfaces:**
- Consumes: `TrackedAnthropic` (Task 2).
- Produces:
  - `Synthesizer.__init__(self, settings=None, llm=None)`; `self.client` is a `TrackedAnthropic(call_site="query.synthesizer")` unless `llm` injected. `synthesize` passes `query_id=query_response.query_id` to `create`.
  - `QueryClassifier.__init__(self, settings=None, llm=None)`; `self.client` call_site `"query.classifier"`. `classify(self, question, query_id: Optional[str] = None)` passes `query_id` to `create`.
  - `Evaluator.__init__(self, settings=None, llm=None)`; `self.client` call_site `"evaluation.evaluator"`. `evaluate` passes `query_id=response.query_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/query/__init__.py` (empty) and `tests/query/test_query_path_instrumentation.py`:

```python
from types import SimpleNamespace

from src.config import Settings
from src.llm.client import TrackedAnthropic
from src.models import QueryResponse
from src.query.classifier import QueryClassifier
from src.query.synthesizer import Synthesizer


class FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text=self._text)],
        )


class FakeClient:
    def __init__(self, text):
        self.messages = FakeMessages(text)


def _tracked(text, call_site, recorded):
    return TrackedAnthropic(Settings(anthropic_api_key="x"), call_site=call_site,
                            client=FakeClient(text), sink=recorded.append)


def test_default_synthesizer_client_has_call_site():
    s = Synthesizer(Settings(anthropic_api_key="x"))
    assert s.client.call_site == "query.synthesizer"


def test_default_classifier_client_has_call_site():
    c = QueryClassifier(Settings(anthropic_api_key="x"))
    assert c.client.call_site == "query.classifier"


def test_synthesize_records_usage_with_query_id():
    recorded = []
    llm = _tracked("Final answer [Source: x, Section: y]", "query.synthesizer", recorded)
    s = Synthesizer(Settings(anthropic_api_key="x"), llm=llm)
    # one vector result so context is non-empty
    results = [SimpleNamespace(store="vector", error=None,
                               chunks=[{"payload": {"text": "t", "source_file": "f"},
                                        "chunk_id": None}],
                               sql_rows=None, graph_data=None)]
    resp = QueryResponse(query_id="55555555-5555-5555-5555-555555555555",
                         question="q", answer="", timestamp="t")
    s.synthesize("q", results, resp)
    assert len(recorded) == 1
    assert recorded[0].query_id == "55555555-5555-5555-5555-555555555555"
    assert recorded[0].call_site == "query.synthesizer"


def test_classify_records_usage_with_query_id():
    recorded = []
    llm = _tracked('{"sources": ["vector"], "execution": "parallel"}', "query.classifier", recorded)
    c = QueryClassifier(Settings(anthropic_api_key="x"), llm=llm)
    c.classify("who runs public works?", query_id="66666666-6666-6666-6666-666666666666")
    assert len(recorded) == 1
    assert recorded[0].query_id == "66666666-6666-6666-6666-666666666666"
    assert recorded[0].call_site == "query.classifier"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/query/test_query_path_instrumentation.py -v`
Expected: FAIL — `Synthesizer`/`QueryClassifier` don't accept `llm=`, and `s.client.call_site` raises `AttributeError`.

- [ ] **Step 3: Refactor `Synthesizer`**

In `src/query/synthesizer.py`:

Replace the `import anthropic` line with:

```python
from src.llm.client import TrackedAnthropic
```

Replace `Synthesizer.__init__` (lines ~38-40):

```python
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="query.synthesizer")
```

In `synthesize`, change the `self.client.messages.create(...)` call (line ~62) to pass `query_id`:

```python
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=4096,
                query_id=query_response.query_id,
                messages=[{
                    "role": "user",
                    "content": _SYNTHESIS_PROMPT.format(
                        question=question,
                        context=context,
                    ),
                }],
            )
```

- [ ] **Step 4: Refactor `QueryClassifier`**

In `src/query/classifier.py`:

Replace the `import anthropic` line with:

```python
from src.llm.client import TrackedAnthropic
```

Replace `QueryClassifier.__init__` (lines ~65-67):

```python
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="query.classifier")
```

Change `classify` signature and the `create` call:

```python
    def classify(self, question: str, query_id: Optional[str] = None) -> QueryPlan:
        """Classify the question and return a retrieval plan."""
        try:
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=1024,
                query_id=query_id,
                messages=[{
                    "role": "user",
                    "content": _CLASSIFY_PROMPT.format(question=question),
                }],
            )
```

- [ ] **Step 5: Refactor `Evaluator`**

In `src/evaluation/evaluator.py`:

Replace the `import anthropic` line with:

```python
from src.llm.client import TrackedAnthropic
```

Replace `Evaluator.__init__` (lines ~64-66):

```python
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="evaluation.evaluator")
```

Change the `create` call in `evaluate` (line ~84) to pass `query_id`:

```python
            msg = self.client.messages.create(
                model=self.cfg.synthesis_model,
                max_tokens=512,
                query_id=response.query_id,
                messages=[{"role": "user", "content": prompt}],
            )
```

- [ ] **Step 6: Thread `query_id` from the pipeline into `classify`**

In `src/query/pipeline.py`, change the classify call in `ask` (line ~63) from:

```python
        plan = self.classifier.classify(question)
```

to:

```python
        plan = self.classifier.classify(question, query_id=query_id)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m pytest tests/query/test_query_path_instrumentation.py -v`
Expected: PASS (4 passed).

- [ ] **Step 8: Run the full suite to confirm no regressions**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/query/synthesizer.py src/query/classifier.py src/evaluation/evaluator.py src/query/pipeline.py tests/query/__init__.py tests/query/test_query_path_instrumentation.py
git commit -m "feat(llm): route query + eval call sites through TrackedAnthropic with query_id"
```

---

### Task 5: Route the ingestion/extraction call sites through `TrackedAnthropic`

Refactors the four offline sites. The two extractor classes construct their client in `__init__`; the two parser/classifier modules construct theirs in module functions, so this task introduces a small `_make_llm(cfg)` factory in each for a testable construction point. Behavior-preserving.

**Files:**
- Modify: `src/extraction/graph_extractor.py` (`GraphExtractor.__init__`)
- Modify: `src/extraction/sql_extractor.py` (`SQLExtractor.__init__`)
- Modify: `src/ingestion/classifier.py` (add `_make_llm`; replace the two `anthropic.Anthropic(...)` constructions)
- Modify: `src/ingestion/parsers/vision_parser.py` (add `_make_llm`; replace the `anthropic.Anthropic(...)` construction)
- Create: `tests/extraction/__init__.py` (empty)
- Create: `tests/extraction/test_ingestion_instrumentation.py`

**Interfaces:**
- Consumes: `TrackedAnthropic` (Task 2).
- Produces:
  - `GraphExtractor.__init__(self, settings=None, llm=None)`; `self.client` call_site `"ingestion.graph_extractor"`.
  - `SQLExtractor.__init__(self, settings=None, llm=None)`; `self.client` call_site `"ingestion.sql_extractor"`.
  - `src/ingestion/classifier.py::_make_llm(cfg) -> TrackedAnthropic` with call_site `"ingestion.classifier"`.
  - `src/ingestion/parsers/vision_parser.py::_make_llm(cfg) -> TrackedAnthropic` with call_site `"ingestion.vision_parser"`.

- [ ] **Step 1: Write the failing test**

Create `tests/extraction/__init__.py` (empty) and `tests/extraction/test_ingestion_instrumentation.py`:

```python
from src.config import Settings
from src.extraction.graph_extractor import GraphExtractor
from src.extraction.sql_extractor import SQLExtractor
from src.ingestion import classifier as ing_classifier
from src.ingestion.parsers import vision_parser


def test_graph_extractor_client_call_site():
    g = GraphExtractor(Settings(anthropic_api_key="x"))
    assert g.client.call_site == "ingestion.graph_extractor"


def test_sql_extractor_client_call_site():
    s = SQLExtractor(Settings(anthropic_api_key="x"))
    assert s.client.call_site == "ingestion.sql_extractor"


def test_ingestion_classifier_make_llm_call_site():
    llm = ing_classifier._make_llm(Settings(anthropic_api_key="x"))
    assert llm.call_site == "ingestion.classifier"


def test_vision_parser_make_llm_call_site():
    llm = vision_parser._make_llm(Settings(anthropic_api_key="x"))
    assert llm.call_site == "ingestion.vision_parser"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/extraction/test_ingestion_instrumentation.py -v`
Expected: FAIL — `g.client.call_site` raises `AttributeError`; `_make_llm` does not exist.

- [ ] **Step 3: Refactor `GraphExtractor`**

In `src/extraction/graph_extractor.py`, replace `import anthropic` with:

```python
from src.llm.client import TrackedAnthropic
```

Replace `GraphExtractor.__init__` (lines ~68-70):

```python
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="ingestion.graph_extractor")
```

(The `self.client.messages.create(...)` call in `extract_batch` is unchanged.)

- [ ] **Step 4: Refactor `SQLExtractor`**

In `src/extraction/sql_extractor.py`, replace `import anthropic` with:

```python
from src.llm.client import TrackedAnthropic
```

Replace `SQLExtractor.__init__` (lines ~114-116):

```python
    def __init__(self, settings: Optional[Settings] = None, llm: Optional[TrackedAnthropic] = None):
        self.cfg = settings or get_settings()
        self.client = llm or TrackedAnthropic(self.cfg, call_site="ingestion.sql_extractor")
```

- [ ] **Step 5: Refactor `src/ingestion/classifier.py`**

Replace `import anthropic` with:

```python
from src.llm.client import TrackedAnthropic
```

Add this factory near the top of the module (after the imports / constants, before `_llm_classify`):

```python
def _make_llm(cfg: Settings) -> TrackedAnthropic:
    return TrackedAnthropic(cfg, call_site="ingestion.classifier")
```

Then replace the two existing client constructions. At line ~72, change:

```python
    llm = client or anthropic.Anthropic(api_key=cfg.anthropic_api_key)
```

to:

```python
    llm = client or _make_llm(cfg)
```

At line ~83, change:

```python
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
```

to:

```python
    client = _make_llm(cfg)
```

(The `_llm_classify(text, client, cfg)` call through `client.messages.create(...)` is unchanged — `TrackedAnthropic` exposes the same surface.)

- [ ] **Step 6: Refactor `src/ingestion/parsers/vision_parser.py`**

Replace `import anthropic` with:

```python
from src.llm.client import TrackedAnthropic
```

Add this factory after the imports:

```python
def _make_llm(cfg: Settings) -> TrackedAnthropic:
    return TrackedAnthropic(cfg, call_site="ingestion.vision_parser")
```

At line ~65, change:

```python
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
```

to:

```python
    client = _make_llm(cfg)
```

(`_extract_page(client, cfg, image, page_num)` and its `client.messages.create(...)` call are unchanged.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m pytest tests/extraction/test_ingestion_instrumentation.py -v`
Expected: PASS (4 passed).

- [ ] **Step 8: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 9: Verify no direct `anthropic.Anthropic(` constructions remain in call sites**

Run:

```bash
grep -rn "anthropic.Anthropic(" src/
```

Expected: no matches in `src/ingestion/classifier.py`, `src/ingestion/parsers/vision_parser.py`, `src/extraction/graph_extractor.py`, `src/extraction/sql_extractor.py`, `src/query/classifier.py`, `src/query/synthesizer.py`, `src/evaluation/evaluator.py`. The only remaining references should be inside `src/llm/client.py` (the wrapper) and any unused construction in `src/ingestion/pipeline.py:47` — if `src/ingestion/pipeline.py:47` (`self._anthropic = ...`) is unused elsewhere in that file (`grep -n "_anthropic" src/ingestion/pipeline.py` shows only the assignment), delete that line in this step and re-run the suite.

- [ ] **Step 10: Commit**

```bash
git add src/extraction/graph_extractor.py src/extraction/sql_extractor.py src/ingestion/classifier.py src/ingestion/parsers/vision_parser.py tests/extraction/__init__.py tests/extraction/test_ingestion_instrumentation.py
git commit -m "feat(llm): route ingestion/extraction call sites through TrackedAnthropic"
```

---

### Task 6: Cost report + `/admin/costs` route + end-to-end smoke

Adds the aggregation query and an HTTP endpoint, then verifies a real call produces a row.

**Files:**
- Modify: `src/storage/sql_store.py` (add `usage_report`)
- Modify: `app.py` (add `/admin/costs` route)
- Create: `tests/test_admin_costs_route.py`

**Interfaces:**
- Consumes: `llm_usage` table (Task 3).
- Produces:
  - `SQLStore.usage_report(self, start: str, end: str) -> list[dict]` — rows grouped by `(call_site, model)` with `calls`, summed token columns, and `est_cost_usd`, ordered by cost desc.
  - `GET /admin/costs?start=<ISO>&end=<ISO>` returning `{"start", "end", "total_cost_usd", "by_call_site": [...]}`.

- [ ] **Step 1: Add `usage_report` to `SQLStore`**

In `src/storage/sql_store.py`, add to the `SQLStore` class (after `get_low_scoring_queries`):

```python
    def usage_report(self, start: str, end: str) -> list[dict[str, Any]]:
        sql = """
            SELECT call_site, model,
                   COUNT(*)                  AS calls,
                   SUM(input_tokens)         AS input_tokens,
                   SUM(output_tokens)        AS output_tokens,
                   SUM(cache_read_tokens)    AS cache_read_tokens,
                   SUM(cache_write_tokens)   AS cache_write_tokens,
                   SUM(est_cost_usd)         AS est_cost_usd
            FROM llm_usage
            WHERE timestamp >= %s AND timestamp < %s
            GROUP BY call_site, model
            ORDER BY est_cost_usd DESC NULLS LAST
        """
        with self.cursor() as cur:
            cur.execute(sql, (start, end))
            return [dict(r) for r in cur.fetchall()]
```

- [ ] **Step 2: Write the failing route test**

Create `tests/test_admin_costs_route.py`:

```python
import app as app_module


class FakeStore:
    def usage_report(self, start, end):
        return [
            {"call_site": "query.synthesizer", "model": "claude-sonnet-4-6",
             "calls": 3, "input_tokens": 300, "output_tokens": 90,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "est_cost_usd": 0.0024},
            {"call_site": "query.classifier", "model": "claude-sonnet-4-6",
             "calls": 3, "input_tokens": 120, "output_tokens": 30,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "est_cost_usd": 0.0008},
        ]


def test_admin_costs_returns_breakdown(monkeypatch):
    monkeypatch.setattr(app_module, "_sql_store", FakeStore())
    monkeypatch.setattr(app_module, "_ready", True)
    client = app_module.app.test_client()
    resp = client.get("/admin/costs?start=2026-06-01&end=2026-07-01")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["start"] == "2026-06-01" and body["end"] == "2026-07-01"
    assert len(body["by_call_site"]) == 2
    assert body["total_cost_usd"] == round(0.0024 + 0.0008, 6)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_admin_costs_route.py -v`
Expected: FAIL with 404 (route not defined) — `assert resp.status_code == 200`.

- [ ] **Step 4: Add the route to `app.py`**

In `app.py`, add this route (place it near the other routes, e.g. after the `/feedback` route):

```python
@app.route("/admin/costs", methods=["GET"])
def admin_costs():
    if not _ready:
        return jsonify({"error": _startup_error or "not ready"}), 503
    start = request.args.get("start", "1970-01-01")
    end = request.args.get("end", "2100-01-01")
    rows = _sql_store.usage_report(start, end)
    total = round(sum(float(r["est_cost_usd"] or 0) for r in rows), 6)
    return jsonify({
        "start": start,
        "end": end,
        "total_cost_usd": total,
        "by_call_site": rows,
    })
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_admin_costs_route.py -v`
Expected: PASS.

> Implementer note: if importing `app` triggers DB/network side effects at import time, the test will error on import. In that case, confirm `app.py` performs initialization inside a function/route (not at module top level). The repo's `app.py` uses lazy globals (`_ready=False`, init in a startup function), so `import app` should be side-effect free. If it is not, wrap the offending top-level init in an `if __name__ == "__main__":` guard as part of this step and note it in the commit.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (all tasks' tests green).

- [ ] **Step 7: End-to-end smoke — confirm real calls write rows**

With a working DB and `ANTHROPIC_API_KEY` set, issue one real query against the running app (or call the pipeline directly), then check the table:

```bash
psql "$(python3 -c 'from src.config import get_settings; print(get_settings().database_url)')" \
  -c "SELECT call_site, model, input_tokens, output_tokens, est_cost_usd, query_id FROM llm_usage ORDER BY timestamp DESC LIMIT 10;"
```

Expected: rows for `query.classifier` and `query.synthesizer` (and `evaluation.evaluator` if the background eval ran) sharing the same `query_id`, each with non-zero `input_tokens` and `est_cost_usd`.

Then check the report endpoint (app running):

```bash
curl -s "http://localhost:8000/admin/costs?start=2026-06-01&end=2026-07-01" | python3 -m json.tool
```

Expected: JSON with `total_cost_usd` > 0 and a `by_call_site` breakdown.

- [ ] **Step 8: Commit**

```bash
git add src/storage/sql_store.py app.py tests/test_admin_costs_route.py
git commit -m "feat(llm): add cost report and /admin/costs endpoint"
```

---

## What this plan does NOT cover (next plans, data-driven)

Per the design spec, Phases 2 and 3 ship only after this instrumentation is live and has produced real `llm_usage` data, because that data decides where the effort is worth spending:

- **Phase 2 plan** — accuracy-neutral reductions: sample the per-query auto-eval (`eval_sample_rate`), add prompt-cache breakpoints on static prefixes (verified via `cache_read_tokens`), and move offline extractors to the Batch API.
- **Phase 3 plan** — eval-gated model right-sizing (classification call sites → Haiku 4.5), gated on the `evaluation/suite.py` results.

Write each as its own plan once the cost breakdown from `/admin/costs` is available.
