# Quarterly-Report Extraction Unification — Design

**Date:** 2026-07-10
**Status:** Proposed
**Related:** `docs/ingestion-workflow.md`, memory `routes-to-sql-gate-drops-structured-data`

## Problem

`quarterly_report` is the only document type that does **not** use the standard
schema-driven extraction path. Instead it runs a special-cased branch in
`pipeline.py` that **pre-filters chunks before extraction**:

- expenditures & metrics come from `extract_chunks_batched(sql_chunks)`, where
  `sql_chunks = [c for c in chunks if c.routes_to_sql()]` — i.e. only chunks the
  content-classifier tagged `table` or `metrics`.
- grants, goals, vacancies come from keyword-filtered focused extractors
  (`"grant"`, `"goal"`, `"vacan"`).

Both pre-filters depend on **report format**, which varies by department. The same
"Department Vacancy Updates" block was tagged `metrics` in the police Q3 report
(→ extracted) and `org_data` in Q1/Q2 (→ silently dropped). Measured impact:
**68% of number-bearing quarterly chunks (1,829 / 2,688) never reached the
extractor**; vacancies were captured in only 16 of 50 reports that contained them,
metrics in 38 of 62 report-periods, expenditures in 23 of 62.

Root cause: **routing structured data by a format-dependent, per-chunk signal.**
No per-department rule set can fix this — the fix must remove the format dependence.

## Goal

Extract structured data from **every** quarterly report accurately, regardless of
how a department formats it, without maintaining per-department or per-table
selection rules.

## Non-goals

- Re-parsing or re-embedding documents (chunks already exist; text is unchanged).
- Changing the other document types (resolution / minutes / legislation / budget
  already use the schema path and are healthy).
- Adding the *other* new-table candidates (personnel actions, community engagement,
  challenges — see "Future schema candidates"). **Projects IS in scope** (new
  `projects` table, populated by this backfill) so we don't pay for a second full
  extraction pass later; the other candidates get their own spec.
- **Wiring the Projects tab to read the new `projects` table.** This change only
  *populates* the table. The tab keeps synthesizing from grants+resolutions until a
  follow-up spec decides whether the table replaces or augments that synthesis.
- Building an agent that decides table placement dynamically (explicitly rejected;
  see memory `routes-to-sql-gate-drops-structured-data`). Placement stays declared.

## Design

### Core change: one schema-driven pass over the whole report

Replace the special-cased SQL portion of the `quarterly_report` branch with the
same mechanism every other type uses — extract against the declared Pydantic
schema (`QuarterlyReportExtraction`) over **all** chunks, with **no
`routes_to_sql()` gate and no keyword pre-filter**. The schema's `sql_targets`
becomes `[expenditures, metrics, grants, vacancies, goals, projects]` — **goals is
folded in** (see "Goals") and a **new `projects` target is added** (see "Projects
table") so all six structured outputs come from one pass and none depend on a
format-specific signal.

Because a quarterly report is a multi-row document (unlike a single resolution),
the pass is **batched**: iterate all chunks in batches (reuse
`extraction_batch_size`), run the schema extraction per batch, and **merge** the
resulting rows across batches. Confidence filtering (`high`/`medium` only) and the
mandatory verbatim `source_text` requirement are kept per row.

Accuracy comes from signals that are **not** format- or department-specific:
1. The **schema** defines the target columns (not the report's layout).
2. Every row must carry a **verbatim `source_text` quote**; no quote → no row.
3. The **confidence filter** drops uncertain rows.
4. The existing **validation gate** (`validate_extraction`) runs on the result.

### Precision rules move from prompt code into schema field descriptions

The dedicated `extract_grants` path was originally added because the *old* generic
extractor mistook budget lines for grants. To preserve that precision in the
unified pass, the strict definitions move into the **Pydantic field descriptions**
of the sub-schemas in `src/ingestion/schemas/quarterly_report.py` (these are
serialized into the extraction prompt via `model_json_schema()`):

- `GrantRow` — "EXTERNAL award to the City (federal/state/county/foundation) with a
  total award amount; NOT a budget line, appropriation, spending figure, or salary."
- `ExpenditureRow` — "from a structured budget/Munis table with an account number;
  not a narrative dollar mention."
- `MetricRow` — "explicitly stated count/total/rate; never inferred or calculated."
- `VacancyRow` — add a `count: Optional[int]` field: "number of open positions for
  this title, e.g. the parenthesized number in 'Patrol Officer- (25)', else null."
- `GoalRow` — carry over `GoalsExtraction`'s fields (`goal_title`, `description`,
  `target`, `status`) with description: "a department objective/priority for the
  period, however the section is labeled (Annual Goals, Objectives, Priorities, or
  an unlabeled list); `target` only if a quantified aim is stated, `status` only if
  progress is stated." **Extraction-side only** — does NOT include `user_status`
  (that is a human-set dashboard field, never extracted; see backfill safeguard).
- `ProjectRow` *(new)* — "a department initiative / special project named in the
  report (e.g. 'Porch Lights & Ring Doorbells', 'Saturation Details', 'Funding a
  Forensic Investigator Position'), with `project_name`, `description`, `status`
  (only if stated), and `funding_source` (grant/fund name only if stated, else '')."

This keeps the rules **declarative, typed, and department-agnostic** — no bespoke
per-table prompt code, addressing the "don't hard-code too much" concern.

### Goals — folded in (this is "smarter goal detection")

Goals moves off its `"goal"` keyword filter into the unified pass. Because the
extractor now reads the whole report and matches by meaning, it detects the goals
section even when a department labels it "Objectives"/"Priorities" or leaves it
unlabeled — the "smarter goal detection" ask, achieved by the same mechanism, with
no new machinery. A `GoalRow` list is added to `QuarterlyReportExtraction`.

### Projects table (new)

The "Special Projects" section of quarterly reports is currently unstructured
(narrative + graph only). The dashboard **Projects tab does not use it** — it
synthesizes projects live from `grants` + `resolutions` (`src/dashboard/projects.py`),
so real department initiatives that are neither a grant nor a resolution are
invisible today. This change captures them:

- **New `projects` table** (`sql/schema.sql` + a `migrate_2026_07_10_projects.sql`):
  `id, department, project_name, description, status, funding_source, quarter, year,
  source_chunk_id, source_file, ingested_at`.
- **`insert_project_rows`** in `sql_store.py`, mirroring `insert_goal_rows`.
- Add `projects` to `delete_structured_rows`' table list for idempotent re-ingest.
- `ProjectRow` added to `QuarterlyReportExtraction`; `projects` added to the
  registry `sql_targets`.

The `projects` table is **purely additive**: grants stay in `grants`, contracts stay
in `resolutions` — no data moves or is duplicated. It only holds the report "Special
Projects" initiatives that have no table today.

**Consumer is out of scope.** The Projects tab keeps its grants+resolutions synthesis
unchanged. When a follow-up spec wires the table in, the intent is **augment, not
replace** — the tab shows grants + contracts + report-stated initiatives, with grants
and contracts remaining first-class. That follow-up must also resolve **overlap**: a
report initiative can *be* an existing grant (e.g. police "Funding a Forensic
Investigator Position" ↔ the Local Law Enforcement Support Grant); the `funding_source`
field exists to link them so the tab doesn't double-count. Populating the table now
means the data is ready and we avoid a second full extraction pass later.

### What is removed

Inside the `quarterly_report` branch of `_store_chunks` (`pipeline.py`):
- the `routes_to_sql()` filter + `extract_chunks_batched` + `_write_sql_data` call;
- the `extract_grants` keyword call;
- the `extract_vacancies` keyword call (added 2026-07-10, now superseded);
- the `extract_goals` keyword call (`goal_texts = [... "goal" in text]`).

The now-unused `SQLExtractor.extract_grants`, `extract_vacancies`, and
`extract_goals` methods and `tests/extraction/test_vacancy_extraction.py` are
deleted as dead code once the unified path is validated. `_parse_extraction_response`
(generic batched parser) becomes unused by any path and is removed.

### What stays

- **Graph extraction** — the `routes_to_graph()` path is unchanged.
- **`goals.user_status` / `user_status_at`** — human-set fields, never touched by
  extraction. Protected in the backfill (see safeguard below).
- **Vacancy `open_count` column** + its migration (`migrate_2026_07_10_vacancy_count.sql`)
  — already applied; the unified `VacancyRow.count` maps to it via
  `insert_vacancy_rows` (already handles `count`→`open_count`).

### Batching & merge details

- Batch size: `cfg.extraction_batch_size` (currently 8 chunks).
- Merge: concatenate rows from all batches per table key; keep `high`/`medium`.
- Cross-batch duplication risk is low (chunks are disjoint); a section split across
  a batch boundary could yield a near-duplicate row. Accepted for v1; revisit only
  if observed in the dry-run.

## Backfill (re-extraction, not re-ingestion)

The fix changes extraction only, so the backfill reads **existing chunks** — no
re-parse, no re-embed.

- Scope: ~63 quarterly reports → ~1 batched extraction sequence per report.
- Per report: `delete_structured_rows(source_file)` (clears expenditures/metrics/
  grants/vacancies/goals/**projects**), then re-extract from stored chunks and insert.
- Script: generalize the proven `scripts/reextract_police_vacancies.py` pattern to
  all quarterly reports and all six tables (`scripts/reextract_quarterly.py`),
  with a `--write` flag; dry-run prints before/after per report.
- **`user_status` safeguard (goals):** before deleting a report's goals, capture
  `(department, year, quarter, goal_title) → (user_status, user_status_at)` for rows
  where `user_status` is set. After re-inserting, re-apply those values by matching
  on the same natural key. Unmatched statuses (title drifted on re-extraction) are
  **logged, not silently dropped**, for manual reconciliation.
- **Rollout order (LLM spend — requires explicit approval before the full run):**
  1. ship + unit-test the code change;
  2. dry-run on 2 reports, eyeball accuracy;
  3. **grant-precision checkpoint** — compare grant rows before/after on a
     grant-heavy report; if precision regresses, add a grant-specific guard before
     proceeding;
  4. **goals-detection checkpoint** — on a report that labels the section something
     other than "Goals," confirm the goals are now captured; confirm `user_status`
     survived on a report that had one set;
  5. run the full backfill.

## Idempotency

`delete_structured_rows` + re-insert makes re-extraction repeatable. Running the
backfill twice yields the same rows. First-time and re-ingest paths are unaffected.

## Testing

- Unit: batched quarterly extraction with a fake LLM returning multi-batch rows →
  asserts rows merged, low-confidence dropped, vacancy `count` populated, goal rows
  captured, project rows captured, grant field-description precision (a budget-line
  input yields no grant row).
- Unit: `user_status` safeguard — capture/re-apply preserves a set status across a
  simulated re-extraction; a drifted title is logged, not dropped.
- Regression: existing `tests/extraction` and `tests/ingestion` suites stay green.
- Manual: the dry-run before/after on real reports is the end-to-end check.

## Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| Grant over-counting returns in the unified pass | Strict `GrantRow` description + grant-precision checkpoint in dry-run before full run |
| One prompt juggling 6 categories loses precision vs. focused prompts | Confidence filter + `source_text` + validation gate; dry-run eyeball; fall back to a focused pass for a specific table only if a real regression shows |
| Backfill wipes human-set goal statuses | `user_status` capture/re-apply safeguard + logging of unmatched statuses |
| Long reports exceed output token budget | Batching (8 chunks/call) bounds each call's output |
| Backfill cost | One-time, ~63 batched extractions, from existing chunks; gated on explicit approval |

## Rollback

Code change is a single branch in `pipeline.py` plus schema descriptions — revert
the commit. Data: re-running the previous ingestion path would restore prior rows,
but since the new data is strictly more complete, rollback is code-only in practice.

## Future schema candidates (NOT in this change)

**Projects** was pulled into this change (see "Projects table"). The remaining
candidates recur in quarterly reports but stay unstructured for now — each is a
**new table = its own spec** with its own dashboard/query surface. Adding any of
them means another full extraction pass (~63 reports), so batch them thoughtfully.
Ranked by value for cross-table synthesis (memory `council-kb-strategic-direction`):

1. **Personnel actions** — hires, promotions, retirements, trainings (e.g. "FARO
   Focus Laser Scanner training attended by six (6) Officers"). Complements
   `vacancies` for a full staffing picture; enables workforce trend analysis.
2. **Community engagement / outreach** — the "Community Engagement" section (events,
   meetings, communications). Grounded but often sparse ("None this quarter");
   good for "what is each department doing for residents" synthesis.
3. **Challenges / issues** *(lowest — verify feasibility)* — departments note
   problems ("Experienced personnel shortages"). Valuable cross-dept signal, but
   fuzzy to structure consistently; risk of subjective extraction. Prototype before
   committing to a table.

## Other future work

- Add a `quarterly_report` validator to `validation.py` (e.g. implausible metric
  magnitudes) — the gate already runs on this path after unification but currently
  has no quarterly validator.
- Preserve `user_status` on the **normal re-ingest path** too (`ingest_document` →
  `delete_structured_rows`), which has the same latent wipe risk the backfill
  safeguard addresses. Generalize the safeguard if re-ingestion becomes routine.
