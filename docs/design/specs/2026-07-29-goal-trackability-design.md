# Goal Trackability Classification — Design Proposal

**Status: proposal — no classification has been run; awaiting owner approval before any LLM spend.**

## Problem

The `goals` table holds 812 rows (~606 logical goals after cross-quarter collapse, 20 department name variants) extracted from quarterly reports. Only 123 rows carry an explicit `target` (115 numeric). The dashboard currently *derives* measurability with a regex heuristic (`goalProgress()` in `redesign.html`: first integer in target/title vs. first integer in the status note), which:

- misses deadline-based goals ("complete METRO conversion by Q3") entirely,
- treats vague aspirations ("maintain and improve community integration") identically to measurable goals,
- occasionally false-positives (any stray number in a title becomes a "target").

We want a first-class notion of **trackable** (has a metric or deadline we can check against quarterly data) vs. **not trackable** (open-ended aspiration), so the Goals tab and the Next-quarter question generator can treat them differently — e.g. only trackable goals get "no reported progress" flags; aspirations get "consider adding a measurable target" prompts instead.

## Can we trust an LLM to classify trackability?

**Yes, with a rubric and a human-confirm loop — not blindly.** Reasons:

- The task is a short-text, well-bounded 3-way classification — much easier than the extraction this pipeline already trusts Haiku with (the document profiler, the query classifier). Errors here are low-stakes: a wrong label changes UI presentation, never the underlying data.
- The clear cases dominate: goals with a numeric target or explicit deadline are mechanically identifiable; pure aspirations ("safeguard constitutional rights") are obvious. The genuinely ambiguous middle ("retain and recruit employees" — trackable via applicant counts?) is ambiguous *for humans too*, which is exactly where the confidence threshold routes to human judgment.
- The repo already has the pattern to copy: the profiler quarantines low-confidence outputs rather than acting on them. Same shape here.

What we should **not** do is let the label silently drive data decisions with no audit trail — hence the `source`/`confirmed` fields below.

## Proposed scheme

### 1. Labels (3-way, collapsing to a trackable boolean)

| Label | Rubric | Example from our data |
|---|---|---|
| `measurable` | Has (or clearly implies) a numeric metric checkable against quarterly data: count, %, $, rate | "Maintain homicide clearance rate above 57% national average" |
| `milestone` | Binary deliverable or deadline whose completion is observable in reports: launch, hire, complete, convert, "by Q4" | "Cease utilization of METRO functions" (done/not done) |
| `aspiration` | Ongoing/open-ended intent with no checkable endpoint: "maintain", "improve", "increase" with no baseline or number | "Safeguard constitutional rights of all citizens" |

`trackable = label in (measurable, milestone)`. The 3-way costs nothing extra and the measurable/milestone split matters downstream (measurable → progress bars & pace checks; milestone → done/not-done status only).

Alongside the label, the classifier extracts `metric_hint` (e.g. "clearance rate, %, target 57") and `deadline` (ISO quarter/date) when present — these seed later quarterly-data joins but are advisory only.

### 2. Rubric-driven classification, AI-suggested + human confirm

- **Prompt**: the rubric above verbatim, 6 few-shot examples drawn from real Harrisburg goals (2 per label), input = `goal_title` + `description` + `target` + latest 2 `status` notes. Structured output: `{label, confidence (0–1), rationale (1 sentence), metric_hint, deadline}`.
- **Model**: Haiku 4.5 (`claude-haiku-4-5`) via the **Batch API** — same cheap-routing tier the classifier/profiler already use per `config.py` convention; model stays config, not hardcoded.
- **Unit of classification**: the logical goal (canonical dept key + normalized title — same `goalKey()` collapse the UI uses), ~606 items. The label is written to **all** rows sharing that key.
- **Confidence routing** (mirrors the profiler's quarantine pattern):
  - `confidence ≥ 0.8` → stored as AI-suggested; shown in UI with an "AI-suggested" tag until a clerk confirms.
  - `confidence < 0.8` → stored as `needs_review`, surfaced in a triage list; no trackable treatment applied.
- **Human confirm**: goal cards get a confirm/override control identical in shape to the existing `user_status` Edit flow (`POST /goals/<id>/trackability`). Confirming or overriding sets `source='human'`. Clerks never *have* to confirm — AI-suggested labels are usable but visibly provisional. The 2026-07-29 clerk meeting flagged goal trackability, so there is a real reviewer for the triage list.

### 3. Schema (additive migration)

```sql
ALTER TABLE goals
  ADD COLUMN trackability            TEXT,      -- measurable | milestone | aspiration | needs_review
  ADD COLUMN trackability_confidence NUMERIC,
  ADD COLUMN trackability_rationale  TEXT,
  ADD COLUMN trackability_metric     TEXT,      -- metric_hint
  ADD COLUMN trackability_deadline   TEXT,
  ADD COLUMN trackability_source     TEXT,      -- 'ai' | 'human'
  ADD COLUMN trackability_at         TIMESTAMP;
```

Survives re-ingestion the same way `user_status` does (columns are ours, not the extractor's); the batch job re-runs only for goal keys with no existing label, so human overrides are never clobbered.

### 4. Validation gate before the full run

Hand-label a stratified sample of **40 goals** (owner or clerk, ~20 min), run the classifier on just those, measure agreement. Proceed to the full run only if agreement ≥ 90% on the cases the human found unambiguous. This costs ~$0.01 and converts "can we trust it" from a guess into a measurement.

## Cost estimate (Haiku 4.5, Batch API — current pricing $1/M input, $5/M output, batch = 50% off)

Per item: ~600 prompt/rubric/few-shot tokens + ~300 goal tokens in, ~80 structured tokens out
→ (900 × $1 + 80 × $5) / 1M ≈ **$0.0013/goal**, halved by batch ≈ **$0.00065**.

| Run | Items | Cost |
|---|---|---|
| Validation pilot (40 goals, non-batch for speed) | 40 | ≈ $0.05 |
| Full classification | ~606 | ≈ **$0.40** (batch) / $0.80 (non-batch) |
| Quarterly top-up (new goals only) | ~50/qtr | ≈ $0.03/qtr |

Even switching to Sonnet 5 for the whole run would be ≈ $1.30 (batch, intro pricing). **Total commitment: well under $1.** The dominant cost is the ~20 minutes of human labeling for the validation gate, which is also the thing that makes the scheme trustworthy.

## What this unlocks (later, separate work)

- Goals tab: filter/segment by trackable; stop rendering meaningless progress affordances on aspirations; "add a measurable target" nudge on aspirations.
- Next-quarter generator: `goal_no_progress` / `goal_stalled` signals fire only on trackable goals (kills a whole class of noisy questions).
- Eventually: join `trackability_metric` against the `metrics` table for automatic progress computation — explicitly out of scope here.

## Out of scope

- No automatic joining of goals to metrics rows (needs the metrics-side cleanup first — see the routes_to_sql and over-extraction known issues).
- No reclassification of human-labeled goals.
- No changes to extraction schemas or ingestion.
