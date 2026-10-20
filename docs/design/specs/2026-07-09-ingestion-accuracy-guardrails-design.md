# Ingestion Accuracy Guardrails — Design

**Date:** 2026-07-09
**Status:** Proposed
**Author:** Council KB team

## Problem

Ingesting the 2026 resolutions surfaced four failures. Investigation showed three of
them share a single root cause, and the fourth is an unrelated robustness bug.

| Res | Symptom | True cause |
|-----|---------|------------|
| 20-2026 | resolution number written as `2026-2026` | garbled OCR text; number field read as `RESOLUTION NO../-2026` |
| 21-2026 | resolution number written as `4-2026` | garbled OCR text; number field read as `RESOLUTION NO. 4 \| -2026` (real number 21-2026) |
| 19-2026 | profiled `unclassified` → quarantined, no structured row | garbled OCR text (`Yy / Aavos / AouoH feuoneN ...`) |
| 8-2026 | resolution row saved, **0 votes** (partial ingest) | a vote value exceeded `votes.vote varchar(10)`; the insert crashed *after* the resolution row committed |

### Root cause (20/21/19): garbled text flows through unchecked

These PDFs carry a **bad embedded OCR text layer** — gibberish made of ordinary ASCII
letters. The current readability gate (`detector._garbled_ratio`) only counts
**non-ASCII** characters, so ASCII gibberish passes it. The pipeline then:

1. treats the doc as a clean text PDF (never escalates to the image-reading Vision parser),
2. classifies and extracts from gibberish, and
3. **writes nonsense to the structured tables without any sanity check** — e.g. a
   resolution number equal to the year (`2026-2026`), which is impossible.

### Root cause (8): non-atomic writes + a too-narrow column

`_write_typed_data` inserts the resolution row, then the vote rows, as separate
committed statements. A DB error on the vote insert leaves a half-ingested document.
The trigger was `votes.vote varchar(10)` overflowing on a longer vote string.

## Goals

- Stop garbled documents from silently producing wrong structured data.
- Never write structured data that fails a basic sanity check; surface it for review instead.
- Make a document's structured write all-or-nothing.
- Do all of the above without escalating every scan to the expensive Vision path.

## Non-goals

- No review **UI** this round — a database flag plus a plain-text report (per decision).
- No change to the query/synthesis side.
- No reliance on filename conventions (they vary by source).

## Design

Three guardrails in the ingestion pipeline, plus two bug-fixes. All parsing/classification
stays as-is for clean documents; the new logic only engages on garbled or invalid docs.

### Guardrail 1 — Real readability check (`src/ingestion/quality.py`, new)

`text_readability(text) -> float` and `is_garbled(text, cfg) -> bool`.

Heuristic, dependency-light, no model call:
- Tokenize into word-like tokens.
- Score = fraction of tokens that are plausible English words, measured primarily by
  **stopword hit-rate** — real City government prose is dense with `the/of/and/to/shall/
  city/council/whereas/resolved`. Genuine text hits many; gibberish hits almost none.
- Secondary signal: fraction of tokens with a normal vowel/consonant structure and length.
- `is_garbled` is true when the score is below `garble_readability_threshold`
  (config, default ~0.35). Tuned against the captured samples: real resolution text
  scores high; the Res 19/20/21 gibberish scores near zero.

### Guardrail 2 — Escalate garbled docs to Vision (`pipeline._parse_with_fallback`)

After any parse, run `is_garbled` on the assembled text. If garbled **and** the parser
used was not already Vision, re-parse once with `vision_parser` (reads the page images,
ignoring the bad text layer). Capped at a single escalation so cost stays bounded and
there is no loop. Gated by `enable_vision_escalation` (config, default true).

### Guardrail 3 — Validate before write, else flag for review (`src/ingestion/validation.py`, new)

After extraction, before any structured insert, validate the extracted rows per document
type. For `resolution`:
- `resolution_number` matches `^\d{1,4}-\d{4}$` **and** its numeric part ≠ the year
  (rejects `2026-2026`).
- at least the core fields present (a subject/vendor or a WHEREAS/RESOLVED body).
- vote count within a plausible range (0–say 15); vote values sanitized (see Bug-fix B).

Outcome:
- **Valid** → write as today.
- **Invalid, or still garbled after Vision, or profiled `unclassified`/low-confidence**
  → do **not** write structured rows; record a review flag. The document's vector chunks
  are still stored (it remains searchable), matching the existing quarantine behavior.

Validators are keyed by document type so other types (minutes, legislation, budget) can
add their own checks later; unknown types get a no-op validator.

### Bug-fix A — Atomic per-document structured write (`src/storage/sql_store.py`, `pipeline`)

Add a transaction boundary so all of one document's structured inserts share a single
connection and commit once at the end, rolling back entirely on any error. `_write_typed_data`
(and the quarterly-report write path) run inside it. A document either fully lands its
structured data or lands none — never a partial like Res 8. Graph writes remain
best-effort and outside the SQL transaction (unchanged).

### Bug-fix B — Widen and sanitize the vote field (`sql/`, `sql_store.insert_vote_rows`)

- Migration: `ALTER TABLE votes ALTER COLUMN vote TYPE varchar(50)`.
- `insert_vote_rows` normalizes/truncates the vote value defensively (mirrors the existing
  `_VARCHAR_LIMITS` sanitizing used on the quarterly-report path) so an over-long value can
  never again crash a document.

### Review flag + report

- New table `review_flags`: `id, source_file, stage (parse|classify|validate),
  reason, detail (the offending value/text), created_at, resolved (bool default false)`.
- Guardrails 1–3 and the existing quarantine path all record a flag here when they withhold
  or downgrade a document.
- `scripts/review_report.py` prints unresolved flags grouped by reason (source file + why +
  the guessed value). This is the "review list" for now.

### Config additions (`src/config.py`)

- `garble_readability_threshold: float = 0.35`
- `enable_vision_escalation: bool = True`
- (reuses existing `profile_confidence_threshold`, `ocr_*` settings)

## Data flow (after)

```
parse (Unstructured / Tesseract)
   -> is_garbled?  --yes-->  re-parse with Vision (once)
   -> profile (type/confidence)
   -> chunk + embed  (always; keeps doc searchable)
   -> extract fields
   -> validate  --fail / unclassified / still garbled-->  review_flags (no structured write)
              \--pass-->  atomic structured write (resolutions + votes in one transaction)
```

## Testing

- `quality`: real gov-text samples score high; the captured Res 19/20/21 gibberish scores
  garbled. Threshold boundary cases.
- `validation`: `2026-2026` and `4-2026`(from a form with no body) rejected; `21-2026`
  accepted; vote-count and vote-value bounds.
- Vote sanitizer: over-long value truncated, not raised.
- Atomicity: a forced vote-insert error leaves **no** resolution row for that doc.
- Report: flagged docs appear with correct reason.

## Rollout / backfill

After the code lands and tests pass, re-ingest the four affected documents
(Res 8, 19, 20, 21) through the improved pipeline. Re-ingest is idempotent (clears prior
rows by `source_file`), so it also removes the bogus `4-2026` / `2026-2026` rows. Expected
extra spend is small (~$0.20; Vision on the three scans + Res 8). Verify: valid numbers
written, Res 8 has its votes, Res 19 no longer unclassified (or is flagged for review, not
silently dropped).

## Cost impact

- Clean documents: unchanged (cheap path).
- Garbled documents: one extra Vision parse each — bounded because it only fires on docs
  that fail the readability check.
- No new per-query cost.
```
