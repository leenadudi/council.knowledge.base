# Follow-up-Aware Queries — Design

**Date:** 2026-07-14
**Status:** Approved (backend scope)
**Scope:** Backend query pipeline only. The dashboard/API thread UI is a
noted follow-on, not part of this spec.

## Problem

The "ask a question" feature is stateless one-shot Q&A: each `ask()` runs
`classify → retrieve → synthesize → log` with no memory between calls. This
fits factual lookups well but forces users into friction on the exploratory
half of real usage — they must re-type full context to ask "what about the
year before?" or "break that down by department."

Usage is **genuinely mixed** (both quick lookups and exploratory digging), so
the goal is to support natural follow-ups *without* becoming a full multi-turn
chat agent — which would raise cost, and risk conversation history bleeding
into grounded answers and undermining the anti-hallucination design (clarity
gate, citations, weak-retrieval fallback).

## Approach

**One-shot underneath, follow-up-aware on top.** Retrieval and synthesis stay
fully stateless. The *only* component that ever sees prior conversation is the
classifier — which already makes exactly one LLM call per question. We augment
that existing call to resolve follow-ups into standalone questions.

**Net cost:** first question in a thread is identical to today. Follow-ups have
identical LLM call count (still 2 total: classify + synthesize), only a
slightly larger classifier prompt. No new LLM calls, ever.

### Rejected alternatives

- **Full multi-turn chat** (history threaded through synthesis): growing token
  cost per turn, and history leaks into the answer where the model can answer
  from conversation instead of retrieved chunks. Rejected — breaks grounding
  integrity.
- **Separate rewrite LLM call before classify:** clean separation but +1 call
  on every follow-up. Rejected — the classifier can do the rewrite in its
  existing call for free.

## Design

### 1. Backend stays stateless; history is passed in, not stored

`QueryPipeline.ask()` gains one optional parameter:

```python
def ask(
    self,
    question: str,
    history: list[dict] | None = None,
    log_query: bool = True,
) -> QueryResponse:
```

- `history` is an ordered list of prior turns, each `{"question": str, "answer": str}`.
- The caller (dashboard/API) owns the thread and passes it in. The pipeline
  never stores anything between calls — statelessness preserved.
- **Cap: last 2 turns.** The pipeline truncates `history` to the most recent 2
  entries before use, to bound classifier prompt tokens. (Constant
  `_MAX_HISTORY_TURNS = 2`.)
- `None` or `[]` → behaves exactly like today.

### 2. Classifier resolves follow-ups into standalone questions

`QueryClassifier.classify()` gains an optional `history` argument:

```python
def classify(
    self,
    question: str,
    history: list[dict] | None = None,
    query_id: str | None = None,
) -> QueryPlan:
```

- When `history` is present, the classify prompt is prepended with the prior
  turn(s) (question + answer) under a clearly delimited "Prior conversation"
  section, followed by the current question.
- The prompt instructs: *If the current question depends on prior context
  (pronouns like "that"/"those", ellipsis, "what about <X>", "break that
  down"), rewrite it into a fully self-contained question. If it is already
  self-contained, or is a fresh unrelated topic, leave it unchanged.*
- The classifier folds the resolved intent into `sql_query` / `vector_query` /
  `graph_query` as it does today, **and** emits the standalone question in a
  new output field.

### 3. New field on `QueryPlan`

```python
resolved_question: Optional[str] = None
```

- Set to the standalone rewrite when the question was a follow-up; set equal to
  the original question when self-contained. `_parse_plan` defaults it to
  `None` when absent (backward compatible with the no-history path).

### 4. Retrieval and synthesis consume the resolved question, never history

In `pipeline.ask()`:

```python
effective_question = plan.resolved_question or question
```

- `effective_question` is passed to synthesis and the weak-retrieval
  `fallback_retrieve`. (Clarity assessment scores retrieval results only and
  takes no question argument — unchanged.)
- Retrieval already runs off the plan's store queries (which the classifier
  built from resolved intent), so it needs no change beyond the fallback call.
- **History is never passed to the retriever or synthesizer.** The only trace
  of prior context that reaches the answer is the rewritten standalone
  question. Citations stay honest.

### 5. Logging

- `QueryResponse.question` continues to store the **original** user question
  (what they typed).
- The query log gains `resolved_question` (via the existing
  `classification` = `plan.model_dump()`, which now includes the field — no
  separate schema change needed). This lets us calibrate follow-up detection
  against real traffic, mirroring the clarity soft-launch pattern.

### 6. Stale-context guard

The guard is the classifier's explicit per-question decision, driven by the
prompt instruction in §2. A fresh unrelated question sets
`resolved_question == question` and behaves identically to today. This is the
highest-risk behavior and gets dedicated tests (below).

## Testing

Unit tests (mock the LLM client, following the existing classifier test
pattern in `tests/query/`):

1. **No history → unchanged behavior.** `classify(q)` and `ask(q)` with no
   history produce the same plan/flow as today; `resolved_question` is `None`
   and `effective_question == question`.
2. **Follow-up carries context.** Given prior turn about fire-dept 2024 and
   current question "what about 2023?", the (mocked) classifier output with
   `resolved_question = "what was the fire dept ... in 2023?"` is threaded into
   synthesis — assert synthesizer receives the resolved question, not "what
   about 2023?".
3. **Fresh topic does NOT carry stale context.** Prior turn about fire-dept;
   current question "who directs Public Works?" → `resolved_question` equals
   (or is semantically) the original; assert history did not distort the store
   plan. (Prompt-behavior test — assert the classifier is *given* the guard
   instruction and that the parse path handles `resolved_question == question`.)
4. **History cap.** `ask()` with 5 history turns truncates to the last 2 before
   calling the classifier.
5. **Backward-compatible parse.** `_parse_plan` on classifier output lacking
   `resolved_question` yields `resolved_question is None`.

## Out of scope (follow-on)

- Dashboard/API thread UI: rendering prior Q&As as a stack, maintaining thread
  state client- or server-side, and passing `history` into `ask()`. This is a
  separate spec that depends on this backend work.
- Turning follow-up detection into an enforced gate or surfacing the rewritten
  question to the user. Soft-launch (log-only) first, per the clarity pattern.

## Files touched

- `src/models.py` — add `resolved_question` to `QueryPlan`.
- `src/query/classifier.py` — `history` arg, prompt augmentation, parse field.
- `src/query/pipeline.py` — `history` arg + cap, `effective_question` threading.
- `tests/query/` — new tests per above.
- **Untouched:** `src/query/retriever.py`, `src/query/synthesizer.py`,
  `src/query/clarity.py`.
