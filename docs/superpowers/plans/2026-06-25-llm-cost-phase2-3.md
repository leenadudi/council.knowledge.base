# LLM Cost Phase 2 + 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut query-path LLM spend ~45% by (Phase 2) sampling the per-query LLM judge instead of running it on 100% of traffic, and (Phase 3) downgrading the query classifier from Sonnet to Haiku **gated by the eval suite** so accuracy is proven to hold.

**Architecture:** Phase 2 adds an `eval_sample_rate` config and a pure `should_sample` gate around the background-eval thread in `app.py`. Phase 3 makes the query-classifier model a per-call-site config value (default unchanged), then flips it to Haiku only if the eval suite (`scripts/evaluate.py`) shows no accuracy regression — which first requires fixing a broken `evaluator.evaluate` call in the suite runner and seeding the (currently empty) `evaluation_suite` table.

**Tech Stack:** Python 3.14, `anthropic` SDK, `psycopg2`, `pydantic-settings`, Flask, `pytest`, the existing `EvaluationSuite` runner.

## Why this scope (data-driven)

A 12-query measurement run populated `llm_usage` and showed query-path spend splits: **synthesizer 42%, classifier 30%, judge 28%** (~$16.82 per 1,000 queries). That data drove three decisions:
- **Sample the judge** (28%, invisible monitoring on 100% of traffic) → ~25% cut, accuracy-neutral. **In scope (Phase 2).**
- **Classifier → Haiku** (30%, simple routing task on Sonnet, 3× cheaper) → ~20% cut, eval-gated. **In scope (Phase 3).**
- **Synthesizer stays Sonnet** (42%, quality-critical answer generation). **Out of scope.**
- **Prompt caching dropped:** classifier (~600 tok) and synthesizer (~1,400 tok) prompts are below Sonnet's 2,048-token minimum cacheable prefix — caching would be a no-op (YAGNI).
- **Batch-API ingestion deferred:** ingestion is infrequent quarterly batches and its cost is unmeasured — revisit if/when ingestion cost data justifies the refactor.

## Global Constraints

- **Behavior-preserving by default:** the Phase 3 config knob (`query_classifier_model`) defaults to the current model (`claude-sonnet-4-6`), so merging this plan changes no behavior until the model is explicitly flipped after the eval gate passes.
- **Phase 3 is eval-gated:** the Haiku swap is kept only if the eval suite shows no regression vs. the Sonnet baseline (criteria in Task 3). If it regresses, revert the default.
- **Sampling is accuracy-neutral:** the per-query judge is background quality monitoring with no user-facing effect; sampling it cannot change any answer returned to a user.
- **The eval-gate run spends real API ($1–2) and needs an API key + reachable cloud stores** (Supabase pgvector + Neo4j Aura). It is run by the operator/controller, not a sandboxed subagent (subagents may lack the key).
- **Test runner:** `python3 -m pytest` from the repo root.

---

### Task 1: Auto-eval sampling (Phase 2)

Gate the background LLM judge so it runs on a configurable fraction of queries instead of all of them.

**Files:**
- Modify: `src/config.py` (add `eval_sample_rate`)
- Modify: `src/evaluation/evaluator.py` (add pure `should_sample` helper)
- Modify: `app.py` (gate the eval thread spawn in `/ask`)
- Create: `tests/evaluation/__init__.py` (empty), `tests/evaluation/test_sampling.py`

**Interfaces:**
- Produces: `should_sample(rate: float, draw: float) -> bool` in `src/evaluation/evaluator.py` (module-level). `Settings.eval_sample_rate: float = 0.1`.

- [ ] **Step 1: Write the failing test**

Create `tests/evaluation/__init__.py` (empty) and `tests/evaluation/test_sampling.py`:

```python
from src.evaluation.evaluator import should_sample


def test_rate_zero_never_samples():
    assert should_sample(0.0, 0.0) is False
    assert should_sample(0.0, 0.999) is False


def test_rate_one_always_samples():
    assert should_sample(1.0, 0.0) is True
    assert should_sample(1.0, 0.999) is True


def test_draw_below_rate_samples():
    assert should_sample(0.1, 0.05) is True


def test_draw_at_or_above_rate_does_not_sample():
    assert should_sample(0.1, 0.1) is False
    assert should_sample(0.1, 0.5) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/evaluation/test_sampling.py -v`
Expected: FAIL with `ImportError: cannot import name 'should_sample'`.

- [ ] **Step 3: Add `eval_sample_rate` to config**

In `src/config.py`, add after the synthesis settings (the line `synthesis_provider: str = "anthropic"`):

```python
    # Per-query LLM judge runs on this fraction of queries (1.0 = every query).
    # Background quality monitoring only — sampling has no user-facing effect.
    eval_sample_rate: float = 0.1
```

- [ ] **Step 4: Add the `should_sample` helper to `evaluator.py`**

In `src/evaluation/evaluator.py`, add at module level (after the imports, before the `_EVAL_PROMPT` constant):

```python
def should_sample(rate: float, draw: float) -> bool:
    """True if this query should be judged. `draw` is a [0,1) random value.

    rate <= 0 never samples; rate >= 1 always samples.
    """
    return draw < rate
```

- [ ] **Step 5: Gate the eval thread in `app.py`**

In `app.py`, ensure these imports exist near the top (add any missing): `import random` and `from src.config import get_settings`. Then in the `/ask` route, replace the unconditional spawn:

```python
        threading.Thread(
            target=_run_evaluation, args=(response,), daemon=True
        ).start()
```

with a sampled spawn:

```python
        from src.evaluation.evaluator import should_sample
        if should_sample(get_settings().eval_sample_rate, random.random()):
            threading.Thread(
                target=_run_evaluation, args=(response,), daemon=True
            ).start()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (existing tests + 4 new sampling tests).

- [ ] **Step 7: Commit**

```bash
git add src/config.py src/evaluation/evaluator.py app.py tests/evaluation/__init__.py tests/evaluation/test_sampling.py
git commit -m "feat(eval): sample per-query LLM judge via eval_sample_rate (default 0.1)"
```

---

### Task 2: Fix the eval-suite runner (Phase 3 gate prerequisite)

The eval suite is the Phase 3 accuracy gate, but it is currently non-functional: `EvaluationSuite.run` calls `evaluator.evaluate(response, context=...)` while `Evaluator.evaluate` takes `retrieved_context=`, so every question throws and is swallowed (0 results). Fix the call so the gate produces real scores.

**Files:**
- Modify: `src/evaluation/suite.py` (fix the `evaluate(...)` kwarg in `run`)
- Create: `tests/evaluation/test_suite_run.py`

**Interfaces:**
- Consumes: `Evaluator.evaluate(self, response, retrieved_context)` (existing), `EvaluationSuite.run(query_pipeline, evaluator)` (existing).

- [ ] **Step 1: Write the failing test**

Create `tests/evaluation/test_suite_run.py`:

```python
from types import SimpleNamespace

from src.config import Settings
from src.evaluation.evaluator import Evaluator
from src.evaluation.suite import EvaluationSuite
from src.llm.client import TrackedAnthropic
from src.models import QueryResponse


class FakeLLMMessages:
    def create(self, **kwargs):
        # Valid evaluator JSON so _parse_score yields a real score
        text = ('{"retrieval_score": 4, "accuracy_score": 5, "completeness_score": 4, '
                '"retrieval_failure": false, "hallucination_detected": false, "reasoning": "ok"}')
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text=text)],
        )


class FakeLLM:
    def __init__(self):
        self.messages = FakeLLMMessages()


class FakePipeline:
    def ask(self, question, log_query=False):
        return QueryResponse(query_id="77777777-7777-7777-7777-777777777777",
                             question=question, answer="Some answer", timestamp="t")


class FakeStore:
    def get_evaluation_suite(self):
        return [{"id": 1, "question": "Who is the Director of Public Works?",
                 "expected_answer": "David West", "store_type": "graph"}]

    def save_evaluation_result(self, run_id, result):
        pass


def test_suite_run_invokes_evaluator_without_typeerror():
    cfg = Settings(anthropic_api_key="x")
    # real Evaluator, but backed by a fake LLM + no-op sink so no network/DB
    evaluator = Evaluator(cfg, llm=TrackedAnthropic(cfg, call_site="evaluation.evaluator",
                                                    client=FakeLLM(), sink=lambda r: None))
    suite = EvaluationSuite(FakeStore(), cfg)
    results = suite.run(FakePipeline(), evaluator)
    # Before the fix, the wrong kwarg raises TypeError, is swallowed, and results is empty.
    assert len(results) == 1
    assert results[0].accuracy_score == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/evaluation/test_suite_run.py -v`
Expected: FAIL — `assert len(results) == 1` fails (results is empty because `evaluate(... context=...)` raised `TypeError` and was swallowed).

- [ ] **Step 3: Fix the kwarg in `suite.py`**

In `src/evaluation/suite.py`, inside `EvaluationSuite.run`, change:

```python
                score = evaluator.evaluate(response, context=response.answer)
```

to:

```python
                score = evaluator.evaluate(response, retrieved_context=response.answer)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/evaluation/test_suite_run.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/evaluation/suite.py tests/evaluation/test_suite_run.py
git commit -m "fix(eval): use retrieved_context kwarg in EvaluationSuite.run so the gate produces scores"
```

---

### Task 3: Per-call-site classifier model + Haiku swap (Phase 3, eval-gated)

Make the query classifier's model a config value (default unchanged → behavior-preserving), then flip it to Haiku **only if** the eval gate passes.

**Files:**
- Modify: `src/config.py` (add `query_classifier_model`)
- Modify: `src/query/classifier.py` (use `query_classifier_model`)
- Create: `tests/query/test_classifier_model.py`

**Interfaces:**
- Consumes: `QueryClassifier` (existing), `TrackedAnthropic` (existing).
- Produces: `Settings.query_classifier_model: str = "claude-sonnet-4-6"`.

- [ ] **Step 1: Write the failing test**

Create `tests/query/test_classifier_model.py`:

```python
from types import SimpleNamespace

from src.config import Settings
from src.llm.client import TrackedAnthropic
from src.query.classifier import QueryClassifier


class RecordingMessages:
    def __init__(self):
        self.models = []

    def create(self, **kwargs):
        self.models.append(kwargs.get("model"))
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=5, output_tokens=2,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text='{"sources": ["vector"], "execution": "parallel"}')],
        )


class RecordingClient:
    def __init__(self):
        self.messages = RecordingMessages()


def test_classifier_uses_query_classifier_model_setting():
    cfg = Settings(anthropic_api_key="x", query_classifier_model="claude-haiku-4-5")
    rec = RecordingClient()
    llm = TrackedAnthropic(cfg, call_site="query.classifier", client=rec, sink=lambda r: None)
    QueryClassifier(cfg, llm=llm).classify("who runs public works?")
    assert rec.messages.models == ["claude-haiku-4-5"]


def test_classifier_model_defaults_to_sonnet():
    assert Settings(anthropic_api_key="x").query_classifier_model == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/query/test_classifier_model.py -v`
Expected: FAIL — `Settings` has no `query_classifier_model`, and the classifier still passes `synthesis_model`.

- [ ] **Step 3: Add the config setting**

In `src/config.py`, add after the synthesis settings:

```python
    # Query classifier model — split from synthesis so the cheap routing task
    # can use a smaller model (Phase 3). Default matches synthesis (behavior-preserving).
    query_classifier_model: str = "claude-sonnet-4-6"
```

- [ ] **Step 4: Use it in the classifier**

In `src/query/classifier.py`, change the `model=` argument in `classify`'s `create` call from:

```python
                model=self.cfg.synthesis_model,
```

to:

```python
                model=self.cfg.query_classifier_model,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (default still Sonnet → behavior-preserving; the Haiku test passes via injected setting).

- [ ] **Step 6: Commit**

```bash
git add src/config.py src/query/classifier.py tests/query/test_classifier_model.py
git commit -m "feat(query): make query classifier model configurable (default sonnet)"
```

- [ ] **Step 7: Eval gate — seed the suite, baseline on Sonnet** *(operator-run; needs API key + cloud stores up)*

Seed the empty `evaluation_suite` table, then run the baseline:

```bash
python3 -c "from src.config import get_settings; from src.storage.sql_store import SQLStore; from src.evaluation.suite import EvaluationSuite; s=SQLStore(get_settings()); EvaluationSuite(s,get_settings()).seed_questions(); s.close()"
python3 scripts/evaluate.py run --log-level WARNING
```

Record the printed **pass rate** and **avg accuracy / retrieval / completeness** scores as the Sonnet baseline.

- [ ] **Step 8: Eval gate — run on Haiku**

Set the classifier to Haiku for this run (env override, no code change):

```bash
query_classifier_model=claude-haiku-4-5 python3 scripts/evaluate.py run --log-level WARNING
```

Record the same metrics.

- [ ] **Step 9: Eval gate — decide (keep or revert)**

**Keep Haiku** (change the `query_classifier_model` default in `src/config.py` to `"claude-haiku-4-5"` and commit) **only if all hold** vs. the Sonnet baseline:
- pass rate is **equal or higher**, and
- avg accuracy_score is **within 0.3** of baseline (not lower by more than 0.3), and
- no single store category (`--store sql|graph|vector|cross`) collapses (re-run a category with `--store` if one looks off).

Otherwise **revert**: leave the default at `"claude-sonnet-4-6"` and record in the report that Haiku regressed routing accuracy. Commit either the default flip or a short note; the eval numbers (baseline vs Haiku) go in the commit message / a results note.

```bash
# If keeping Haiku:
git add src/config.py
git commit -m "feat(query): default query classifier to Haiku 4.5 (eval gate passed: <baseline vs haiku numbers>)"
```

---

## What this plan does NOT cover

- **Prompt caching** — dropped: prefixes are below Sonnet's 2,048-token cache minimum (no-op for this workload).
- **Batch-API ingestion** — deferred: ingestion is infrequent and its cost is unmeasured; revisit when ingestion-path `llm_usage` data justifies the refactor.
- **Ingestion-classifier / vision / extractor model right-sizing** — deferred: those are ingestion-path costs not captured in the query-path measurement; right-size them only after measuring an ingestion run.
- **The two retrieval bugs** found during measurement (grants `quarter` column, graph Cypher `$department_name` param) — separate correctness fixes, not cost work.
