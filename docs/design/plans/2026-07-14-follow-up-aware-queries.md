# Follow-up-Aware Queries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the query pipeline resolve follow-up questions ("what about 2023?") into standalone questions using prior conversation, without adding LLM calls or leaking history into grounded answers.

**Architecture:** Retrieval and synthesis stay stateless. Only the classifier — which already makes one LLM call per question — is augmented to accept prior turns and emit a `resolved_question`. The pipeline passes short history into the classifier and threads `plan.resolved_question or question` into synthesis and the weak-retrieval fallback. History never reaches the retriever or synthesizer.

**Tech Stack:** Python 3.14, Pydantic models, pytest. LLM via `TrackedAnthropic` (mocked in tests).

## Global Constraints

- No new LLM calls on any path: first question in a thread is identical to today; follow-ups add only tokens to the existing classifier call.
- History is passed in per-call, never stored on the pipeline. Cap: last 2 turns (`_MAX_HISTORY_TURNS = 2`).
- Retriever (`src/query/retriever.py`), synthesizer (`src/query/synthesizer.py`), and clarity (`src/query/clarity.py`) are **untouched**.
- `QueryResponse.question` continues to store the **original** user question.
- Backward compatible: no-history path behaves exactly as today; `resolved_question` defaults to `None` when the classifier omits it.
- Reference spec: `docs/superpowers/specs/2026-07-14-follow-up-aware-queries-design.md`.

## File Structure

- `src/models.py` — add one field `resolved_question` to `QueryPlan`.
- `src/query/classifier.py` — add `history` arg to `classify()`, prompt augmentation (prior-conversation block + `resolved_question` output field + follow-up/guard instruction), parse the new field, and a `_build_prior_conversation` helper.
- `src/query/pipeline.py` — add `history` arg to `ask()`, cap to last 2 turns, compute `effective_question = plan.resolved_question or question`, thread it into `synthesize()` and `fallback_retrieve()`.
- `tests/query/test_followup_classifier.py` — new: classifier history + resolved_question behavior.
- `tests/query/test_followup_pipeline.py` — new: pipeline history threading + cap.

---

### Task 1: Add `resolved_question` to `QueryPlan` and parse it

**Files:**
- Modify: `src/models.py:144-152` (`QueryPlan`)
- Modify: `src/query/classifier.py:112-131` (`_parse_plan`)
- Test: `tests/query/test_followup_classifier.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `QueryPlan.resolved_question: Optional[str]` (default `None`); `_parse_plan(raw: str) -> QueryPlan` now populates it from `data.get("resolved_question")`.

- [ ] **Step 1: Write the failing test**

Create `tests/query/test_followup_classifier.py`:

```python
from src.query.classifier import _parse_plan


def test_parse_plan_defaults_resolved_question_to_none():
    plan = _parse_plan('{"sources": ["vector"], "execution": "parallel"}')
    assert plan.resolved_question is None


def test_parse_plan_reads_resolved_question_when_present():
    raw = (
        '{"sources": ["sql"], "execution": "parallel", '
        '"resolved_question": "what was the fire dept allocation in 2023?"}'
    )
    plan = _parse_plan(raw)
    assert plan.resolved_question == "what was the fire dept allocation in 2023?"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/query/test_followup_classifier.py -v`
Expected: FAIL — `TypeError`/`ValidationError` (QueryPlan has no `resolved_question`) or `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

In `src/models.py`, add the field to `QueryPlan` (after `reasoning`):

```python
class QueryPlan(BaseModel):
    sources: list[str] = Field(description="Stores to query: sql, vector, graph")
    execution: str = Field(description="parallel or sequential")
    sequential_order: Optional[list[str]] = None
    sql_query: Optional[str] = None
    vector_query: Optional[str] = None
    graph_query: Optional[str] = None
    metadata_filters: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""
    resolved_question: Optional[str] = None
```

In `src/query/classifier.py`, `_parse_plan`, add the field to the returned `QueryPlan` (both the success path and keep the two error-path returns as-is — they default `resolved_question` to `None`):

```python
    return QueryPlan(
        sources=data.get("sources", ["vector"]),
        execution=data.get("execution", "parallel"),
        sequential_order=data.get("sequential_order"),
        sql_query=data.get("sql_query"),
        vector_query=data.get("vector_query"),
        graph_query=data.get("graph_query"),
        metadata_filters=data.get("metadata_filters", {}),
        reasoning=data.get("reasoning", ""),
        resolved_question=data.get("resolved_question"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/query/test_followup_classifier.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/models.py src/query/classifier.py tests/query/test_followup_classifier.py
git commit -m "feat(query): add resolved_question field to QueryPlan"
```

---

### Task 2: Classifier accepts history and augments the prompt

**Files:**
- Modify: `src/query/classifier.py:20-80` (`_CLASSIFY_PROMPT`), `88-109` (`classify`)
- Test: `tests/query/test_followup_classifier.py` (append)

**Interfaces:**
- Consumes: `QueryPlan.resolved_question` (Task 1).
- Produces: `QueryClassifier.classify(self, question: str, history: list[dict] | None = None, query_id: str | None = None) -> QueryPlan`. Each history entry is `{"question": str, "answer": str}`. Adds module-level helper `_build_prior_conversation(history: list[dict] | None) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/query/test_followup_classifier.py`:

```python
from types import SimpleNamespace

from src.config import Settings
from src.llm.client import TrackedAnthropic
from src.query.classifier import QueryClassifier, _build_prior_conversation, _CLASSIFY_PROMPT


class _CapturingMessages:
    def __init__(self):
        self.prompts = []

    def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=5, output_tokens=2,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(text='{"sources": ["sql"], "execution": "parallel"}')],
        )


class _CapturingClient:
    def __init__(self):
        self.messages = _CapturingMessages()


def _classifier_with_capture():
    cfg = Settings(anthropic_api_key="x")
    rec = _CapturingClient()
    llm = TrackedAnthropic(cfg, call_site="query.classifier", client=rec, sink=lambda r: None)
    return QueryClassifier(cfg, llm=llm), rec


def test_prompt_defines_resolved_question_and_followup_guard():
    # The prompt must instruct the model to emit resolved_question and to only
    # carry context when the question actually depends on it.
    assert "resolved_question" in _CLASSIFY_PROMPT
    assert "self-contained" in _CLASSIFY_PROMPT.lower()


def test_build_prior_conversation_empty_when_no_history():
    assert _build_prior_conversation(None) == ""
    assert _build_prior_conversation([]) == ""


def test_build_prior_conversation_includes_turns():
    block = _build_prior_conversation([
        {"question": "fire dept budget 2024?", "answer": "$5M"},
    ])
    assert "fire dept budget 2024?" in block
    assert "$5M" in block


def test_classify_without_history_omits_prior_conversation_block():
    clf, rec = _classifier_with_capture()
    clf.classify("who directs public works?")
    prompt = rec.messages.prompts[0]
    assert "Prior conversation" not in prompt


def test_classify_with_history_injects_prior_conversation():
    clf, rec = _classifier_with_capture()
    clf.classify(
        "what about 2023?",
        history=[{"question": "fire dept budget 2024?", "answer": "$5M"}],
    )
    prompt = rec.messages.prompts[0]
    assert "Prior conversation" in prompt
    assert "fire dept budget 2024?" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/query/test_followup_classifier.py -v`
Expected: FAIL — `ImportError` (`_build_prior_conversation` not defined) and prompt assertions fail.

- [ ] **Step 3: Write minimal implementation**

In `src/query/classifier.py`, edit `_CLASSIFY_PROMPT`. (a) Add `resolved_question` to the JSON schema block — insert this line right after the `"reasoning": "..."` line inside the `{{ }}` object:

```
  "reasoning": "...",
  "resolved_question": "the question rewritten as a fully standalone question, or the original question unchanged if it is already self-contained"
```

(b) Add a follow-up instruction to the `Rules:` list (append as a new bullet):

```
- FOLLOW-UPS: If a "Prior conversation" section is present AND the user question depends on it (pronouns like "that"/"those"/"it", ellipsis, "what about <X>", "break that down"), rewrite the question into a fully self-contained question in `resolved_question` and build the store queries from that rewrite. If the question is already self-contained or is a fresh unrelated topic, set `resolved_question` to the original question and ignore the prior conversation.
```

(c) Replace the tail `User question: {question}` with a prior-conversation placeholder ahead of it:

```
{prior_conversation}User question: {question}

Return ONLY the JSON object, no explanation.
```

Add the helper (module level, after `_CLASSIFY_PROMPT`):

```python
def _build_prior_conversation(history: Optional[list[dict]]) -> str:
    """Render prior turns as a delimited prompt block, or '' when none."""
    if not history:
        return ""
    lines = ["Prior conversation (most recent last):"]
    for turn in history:
        q = str(turn.get("question", "")).strip()
        a = str(turn.get("answer", "")).strip()
        lines.append(f"- Q: {q}\n  A: {a}")
    return "\n".join(lines) + "\n\n"
```

Update `classify` to accept and use `history`:

```python
    def classify(
        self,
        question: str,
        history: Optional[list[dict]] = None,
        query_id: Optional[str] = None,
    ) -> QueryPlan:
        """Classify the question and return a retrieval plan."""
        try:
            prompt = _CLASSIFY_PROMPT.format(
                question=question,
                prior_conversation=_build_prior_conversation(history),
            )
            msg = self.client.messages.create(
                model=self.cfg.query_classifier_model,
                max_tokens=1024,
                query_id=query_id,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text
            return _parse_plan(raw)
        except Exception as e:
            logger.error("Query classification failed: %s — defaulting to vector-only", e)
            return QueryPlan(
                sources=["vector"],
                execution="parallel",
                vector_query=question,
                reasoning=f"Classification error: {e}",
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/query/test_followup_classifier.py -v`
Expected: PASS (all tests in file). Then run the full query suite to confirm no regression: `python3 -m pytest tests/query/ -q` — expected all pass.

- [ ] **Step 5: Commit**

```bash
git add src/query/classifier.py tests/query/test_followup_classifier.py
git commit -m "feat(query): classifier resolves follow-ups from prior conversation"
```

---

### Task 3: Pipeline threads history and uses the resolved question

**Files:**
- Modify: `src/query/pipeline.py:24-27` (module constants), `47-98` (`ask`)
- Test: `tests/query/test_followup_pipeline.py`

**Interfaces:**
- Consumes: `QueryClassifier.classify(question, history=...)` (Task 2), `QueryPlan.resolved_question` (Task 1).
- Produces: `QueryPipeline.ask(self, question: str, history: list[dict] | None = None, log_query: bool = True) -> QueryResponse`. Module constant `_MAX_HISTORY_TURNS = 2`.

- [ ] **Step 1: Write the failing test**

Create `tests/query/test_followup_pipeline.py`:

```python
from unittest.mock import MagicMock

from src.models import QueryPlan, QueryResponse, RetrievalResult
from src.query.pipeline import QueryPipeline, _MAX_HISTORY_TURNS


def _pipeline_with_mocks(plan: QueryPlan):
    pipe = QueryPipeline.__new__(QueryPipeline)
    pipe.cfg = MagicMock()
    pipe.classifier = MagicMock()
    pipe.classifier.classify.return_value = plan
    pipe.retriever = MagicMock()
    pipe.retriever.retrieve.return_value = [
        RetrievalResult(store="vector", chunks=[{"score": 0.9, "payload": {}}]),
    ]
    pipe.retriever.fallback_retrieve.return_value = []
    pipe.synthesizer = MagicMock()
    pipe.synthesizer.synthesize.side_effect = (
        lambda q, results, resp: resp
    )
    pipe.sql_store = MagicMock()
    return pipe


def test_ask_without_history_passes_none_and_original_question():
    plan = QueryPlan(sources=["vector"], execution="parallel")
    pipe = _pipeline_with_mocks(plan)
    pipe.ask("who directs public works?", log_query=False)
    pipe.classifier.classify.assert_called_once()
    assert pipe.classifier.classify.call_args.kwargs.get("history") is None
    # synthesizer receives the original question (resolved_question is None)
    assert pipe.synthesizer.synthesize.call_args.args[0] == "who directs public works?"


def test_ask_uses_resolved_question_for_synthesis():
    plan = QueryPlan(
        sources=["sql"], execution="parallel",
        resolved_question="fire dept allocation in 2023?",
    )
    pipe = _pipeline_with_mocks(plan)
    pipe.ask(
        "what about 2023?",
        history=[{"question": "fire dept allocation in 2024?", "answer": "$5M"}],
        log_query=False,
    )
    # synthesizer sees the resolved standalone question, not "what about 2023?"
    assert pipe.synthesizer.synthesize.call_args.args[0] == "fire dept allocation in 2023?"


def test_ask_caps_history_to_last_two_turns():
    plan = QueryPlan(sources=["vector"], execution="parallel")
    pipe = _pipeline_with_mocks(plan)
    history = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(5)]
    pipe.ask("follow up", history=history, log_query=False)
    passed = pipe.classifier.classify.call_args.kwargs["history"]
    assert len(passed) == _MAX_HISTORY_TURNS
    assert passed == history[-_MAX_HISTORY_TURNS:]


def test_ask_preserves_original_question_on_response():
    plan = QueryPlan(
        sources=["sql"], execution="parallel",
        resolved_question="fire dept allocation in 2023?",
    )
    pipe = _pipeline_with_mocks(plan)
    resp = pipe.ask(
        "what about 2023?",
        history=[{"question": "fire dept allocation in 2024?", "answer": "$5M"}],
        log_query=False,
    )
    assert resp.question == "what about 2023?"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/query/test_followup_pipeline.py -v`
Expected: FAIL — `ImportError` on `_MAX_HISTORY_TURNS`, and `ask()` has no `history` kwarg.

- [ ] **Step 3: Write minimal implementation**

In `src/query/pipeline.py`, add the constant near the top (after `_MIN_USEFUL_CHUNKS`):

```python
# Cap on prior turns passed into the classifier (bounds prompt tokens).
_MAX_HISTORY_TURNS = 2
```

Replace the `ask` signature and the classify/synthesize/fallback calls:

```python
    def ask(
        self,
        question: str,
        history: Optional[list[dict]] = None,
        log_query: bool = True,
    ) -> QueryResponse:
        """
        Full query pipeline: classify → retrieve → synthesize → log.
        Total synchronous LLM calls: 2 (classification + synthesis).

        `history` is an optional list of prior turns
        ({"question", "answer"}); only the last _MAX_HISTORY_TURNS are used, and
        only the classifier sees them — retrieval and synthesis stay stateless.
        """
        start_ms = time.time()
        query_id = str(uuid.uuid4())

        capped_history = history[-_MAX_HISTORY_TURNS:] if history else None

        response = QueryResponse(
            query_id=query_id,
            question=question,
            answer="",
            timestamp=datetime.utcnow().isoformat(),
        )

        # Step 1: Query classification (1 LLM call)
        plan = self.classifier.classify(question, history=capped_history, query_id=query_id)
        logger.info(
            "Query plan — sources: %s, execution: %s",
            plan.sources, plan.execution,
        )

        # A follow-up may have been rewritten into a standalone question; use it
        # for retrieval fallback and synthesis. History itself never flows past here.
        effective_question = plan.resolved_question or question

        # Step 2: Retrieval (0 LLM calls)
        results = self.retriever.retrieve(plan)

        # Check for weak retrieval and fall back if needed
        if _is_weak_retrieval(results):
            results.extend(self.retriever.fallback_retrieve(effective_question, results))

        # Clarity assessment (soft launch: logged only, gate not enforced).
        clarity = assess_retrieval(results, self.cfg)
        if clarity["would_flag"]:
            logger.info(
                "CLARITY would_flag — reasons=%s top=%.3f mean=%.3f header_ratio=%.2f q=%r",
                clarity["reasons"], clarity["top_score"], clarity["mean_score"],
                clarity["header_ratio"], effective_question[:80],
            )

        # Step 3: Synthesis (1 LLM call)
        response = self.synthesizer.synthesize(effective_question, results, response)

        elapsed_ms = int((time.time() - start_ms) * 1000)
        response.total_time_ms = elapsed_ms

        # Log the query asynchronously (best-effort)
        if log_query:
            try:
                self._log_query(response, plan, results, clarity)
            except Exception as e:
                logger.warning("Query logging failed: %s", e)

        return response
```

Note: `response.question` stays the original `question` (set at construction), so the log records what the user typed; `plan.resolved_question` is captured in the log via `plan.model_dump()` in `_log_query`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/query/test_followup_pipeline.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/query/pipeline.py tests/query/test_followup_pipeline.py
git commit -m "feat(query): thread history into classifier, synthesize resolved question"
```

---

### Task 4: Full-suite regression check

**Files:** none (verification only).

- [ ] **Step 1: Run the whole query suite**

Run: `python3 -m pytest tests/query/ -q`
Expected: all pass (existing + new).

- [ ] **Step 2: Run the dashboard query-path tests that exercise the pipeline**

Run: `python3 -m pytest tests/dashboard/test_questions_route.py tests/query/test_query_path_instrumentation.py -q`
Expected: all pass — confirms the added `ask()` kwarg didn't break existing callers (which call `ask(question)` positionally).

- [ ] **Step 3: Commit (only if any test needed a touch-up)**

If everything passed with no changes, skip. Otherwise:

```bash
git add -A
git commit -m "test(query): regression fixes for follow-up-aware pipeline"
```

---

## Self-Review

**Spec coverage:**
- §1 stateless, history passed in, cap 2 → Task 3 (`_MAX_HISTORY_TURNS`, `capped_history`). ✓
- §2 classifier resolves follow-ups, `history` arg, prompt augmentation → Task 2. ✓
- §3 `resolved_question` field, backward-compatible parse → Task 1. ✓
- §4 retrieval/synthesis use resolved question, history never passed downstream → Task 3 (`effective_question`; retriever/synthesizer untouched). ✓
- §5 `QueryResponse.question` = original; `resolved_question` in log via `plan.model_dump()` → Task 3 note. ✓
- §6 stale-context guard = classifier decision → Task 2 prompt bullet + `test_classify_without_history_omits_prior_conversation_block`. ✓
- Testing §1–5 → Tasks 1–3 tests map to: no-history-unchanged (T3), follow-up carries context (T3), fresh topic no stale context (T2 prompt/guard + no-history behavior), history cap (T3), backward-compatible parse (T1). ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code. ✓

**Type consistency:** `resolved_question: Optional[str]` used identically in models, parse, and pipeline; `history: list[dict] | None` consistent across `classify` and `ask`; `_build_prior_conversation`, `_MAX_HISTORY_TURNS` names match between definition and use. ✓
