# LLM Cost Reduction — Design

**Date:** 2026-06-25
**Status:** Approved (design); pending implementation plan
**Goal:** Reduce Anthropic API dollar spend while keeping answer/extraction accuracy within a measured threshold.

## Problem

Every LLM call in the knowledge base goes to Anthropic (`claude-sonnet-4-6`) through code
that constructs its own `anthropic.Anthropic()` client and calls `client.messages.create(...)`
directly. There are seven such call sites:

**Offline ingestion / extraction** (batchable, not latency-sensitive):
- `src/ingestion/classifier.py` — document-type classification
- `src/ingestion/parsers/vision_parser.py` — vision OCR/parse of PDFs
- `src/extraction/graph_extractor.py` — entity/relationship extraction
- `src/extraction/sql_extractor.py` — structured-data extraction

**Online query** (latency-sensitive):
- `src/query/classifier.py` — query classification / store routing
- `src/query/synthesizer.py` — final answer synthesis

**Evaluation:**
- `src/evaluation/evaluator.py` — LLM-as-judge, currently run on **100% of production
  queries** via a background thread spawned at `app.py:112`.

Cost has never been measured, so we do not know which call sites dominate spend. Committing
to aggressive substitution (replacing LLM calls with embeddings/rules) or self-hosted models
before measuring would be optimizing blind and risks trading accuracy for savings in the
wrong place.

## Approach (chosen: "A — measure, then harvest the safe wins")

Three phases. Phase 1 makes spend visible. Phase 2 ships changes that are accuracy-neutral by
construction. Phase 3 makes the one risk-bearing change (model right-sizing) and gates it on
the existing eval suite. Phases 2 and 3 ship only after Phase 1 instrumentation is live, so
each change's savings is observable.

Two alternatives were considered and deferred:
- **B — aggressive substitution** (replace LLM classifiers with Voyage-embedding / rule
  routing). Larger savings, real accuracy risk on routing edge cases. Revisit only if
  instrumentation shows classification is a meaningful slice of spend.
- **C — self-hosted/open models.** Cuts Anthropic spend directly but adds infra + ops cost
  and accuracy risk; at current scale the overhead likely exceeds the savings, and the goal
  is dollar cost, not vendor independence. Shelved.

## Non-goals

- Reducing latency, rate-limit pressure, or vendor dependence (cost is the sole objective).
- Replacing any LLM call with a non-LLM method (that is Approach B, deferred).
- Changing retrieval logic, the tri-store architecture, or the query clarity feature.

---

## Phase 1 — Single LLM chokepoint + cost instrumentation

### Component: `src/llm/client.py` — `TrackedAnthropic`

A thin wrapper exposing the same `messages.create(...)` surface as the Anthropic SDK. It:

- accepts a `call_site` label (e.g. `"ingestion.graph_extractor"`, `"query.synthesizer"`);
- delegates to a single underlying `anthropic.Anthropic` client;
- after each call, reads `response.usage` — `input_tokens`, `output_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`;
- computes `est_cost_usd` from a per-model price table keyed by model ID (input / output /
  cache-read / cache-write per MTok; e.g. `claude-sonnet-4-6` = $3 / $15, `claude-haiku-4-5`
  = $1 / $5, cache-read ≈ 0.1×, cache-write ≈ 1.25×);
- records `{timestamp, call_site, model, input_tokens, output_tokens, cache_read_tokens,
  cache_write_tokens, est_cost_usd, latency_ms, query_id?, batch_id?}`.

The wrapper is the single seam that Phases 2–3 turn knobs on (cache breakpoints, batch
toggle, per-site model), instead of editing seven files each time.

### Call-site refactor

The seven call sites stop constructing their own client and instead receive the wrapper and
pass their `call_site` label. Model, prompts, and parameters are otherwise unchanged — this
step changes no outputs.

### Persistence: `llm_usage` table

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
    query_id            UUID,        -- nullable; links query-path calls to query_logs
    batch_id            VARCHAR(64)  -- nullable; set when served via Batch API (Phase 2)
);
```

- Writes are **best-effort and non-blocking**, matching the async query-logging pattern at
  `src/query/pipeline.py:91`. Instrumentation must never slow or break a real call; a failed
  write is logged and swallowed.
- `query_id` lets query-path LLM cost be joined back to the causing query in `query_logs`.

### Reporting

A cost report aggregating `est_cost_usd` **by call_site** and **by model** over a date range,
exposed as a script and/or an `/admin/costs` route on the existing Flask app. This is the
signal that directs Phase 2/3 effort and confirms each subsequent change's savings.

### Phase 1 acceptance

- Every one of the seven call sites routes through `TrackedAnthropic`.
- A row lands in `llm_usage` for each call, with non-zero cost and correct `call_site`.
- The cost report returns a per-call-site and per-model breakdown.
- A representative ingestion run and a representative query produce no behavioral change vs.
  the pre-refactor baseline.

---

## Phase 2 — Accuracy-neutral reductions

Each item ships after Phase 1 and is verified against the `llm_usage` data.

1. **Sample the per-query auto-eval.** Add `eval_sample_rate` to config (default `0.1`).
   `app.py` gates the background `_run_evaluation` thread on a random draw against this rate.
   The judge is invisible quality monitoring, so sampling has **zero user-facing accuracy
   impact**; it removes roughly one-third of query-path calls (classify + synthesize + judge
   → classify + synthesize + occasional judge).

2. **Prompt caching.** Add `cache_control` breakpoints on the static prompt prefixes — the
   long extraction/synthesis instruction templates and the synthesizer system prompt. Verify
   real cache hits via the `cache_read_tokens` column from Phase 1 (target: non-zero reads on
   repeated-prefix calls). Accuracy-neutral.

3. **Batch API for ingestion.** Route the four offline extractors (classifier, vision, graph,
   sql) through `client.messages.batches.create` — 50% off, and ingestion is not
   latency-sensitive. Larger refactor (submit + poll + collect by `custom_id`); the
   `batch_id` column already exists to attribute these rows. Sequenced last in the phase.

### Phase 2 acceptance

- Auto-eval runs on approximately `eval_sample_rate` of queries; per-query LLM call count
  drops accordingly; eval scores still accumulate in `query_logs`/`evaluation_results`.
- `llm_usage.cache_read_tokens` is non-zero on repeated-prefix calls after caching is added.
- Ingestion runs through the Batch API; `llm_usage` rows for ingestion call sites carry a
  `batch_id` and show ~50% lower `est_cost_usd` per equivalent token volume.

---

## Phase 3 — Eval-gated model right-sizing

- Move the two **classification** call sites (`src/ingestion/classifier.py`,
  `src/query/classifier.py`) from `claude-sonnet-4-6` to `claude-haiku-4-5` (3× cheaper).
  Model becomes a per-call-site config value (enabled by the Phase 1 wrapper).
- **Gate on the eval suite** (`src/evaluation/suite.py`, ~50 seed Q&A pairs): run it before
  and after the swap; keep the change only if accuracy stays within an agreed threshold
  (threshold to be set at implementation time, e.g. no regression beyond a small margin on
  the classification-dependent categories).
- Synthesis and vision stay on Sonnet unless instrumentation shows they dominate cost **and**
  the eval suite proves a downgrade holds.

### Phase 3 acceptance

- Classification call sites run on Haiku 4.5; `llm_usage` confirms the model and lower cost.
- The eval suite shows accuracy within the agreed threshold of the Sonnet baseline; if not,
  the swap is reverted and the result recorded.

---

## Configuration summary (new settings)

- `eval_sample_rate: float = 0.1` — fraction of queries that trigger the LLM judge.
- Per-call-site model settings (replacing the shared `synthesis_model` for everything but
  synthesis), e.g. `doc_classifier_model`, `query_classifier_model`, defaulting to current
  values so Phase 1 is behavior-preserving.
- Per-model price table (in `src/llm/client.py` or config) for cost estimation.

## Risks & mitigations

- **Instrumentation overhead/failure** → best-effort, non-blocking writes; never on the
  request critical path.
- **Cost estimate drift** if Anthropic pricing changes → single price table, easy to update;
  values are explicitly estimates (`est_cost_usd`).
- **Phase 3 accuracy regression** → eval-gated; revert on failure.
- **Prompt-cache silent misses** (a volatile prefix byte defeats caching) → verified via
  `cache_read_tokens`, not assumed.
