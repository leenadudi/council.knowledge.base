# Integration Orchestration Runbook — landing all in-flight work into `main`

**Date:** 2026-08-03
**Author:** schema-relationships session (reconstructed from git — I cannot see the other Claude tabs directly)
**Status:** Advisory runbook. Nothing here has been executed. No branch has been merged or rebased.

**Readiness (per owner, 2026-08-03):** streams 1, 2, 3, 5, 6 are **DONE / ready to integrate**. Stream 4 (`goal-trackability`, "improve dashboard goals") is **still in progress** → land it LAST (it rebases onto everything once done; it's the only stream stacked on stream 1, so deferring it removes the sole cross-dependency). Stream 7 (uncommitted "next-quarter questions") — ownership unconfirmed; must be committed to its own branch before `dev` is touched.

---

## 0. Ground truth (verified read-only from the repo)

- **Integration target is `origin/main` (`42d4db7`).** Your **local `main` is stale — 42 commits behind** `origin/main`. Do not merge onto local `main`; refresh it first (§2, P0).
- **Mechanism = PRs** (matches your history: merged PR #2/#3/#4). Each stream → its own branch → PR → `main`.
- **Seven work streams**, six of them branches, one uncommitted:

| # | Stream (branch) | Size | Base commit | Touches (risk files) | Own migration |
|---|---|---|---|---|---|
| 1 | **`dev`** — schema relationships + entity dimensions | ~18 commits | `4a2b50e` (stale) | `sql_store.py`, `schema.sql`, `pipeline.py` | `migrate_2026_07_28_schema_relationships.sql` |
| 2 | **`worktree-legislative-status-tracking`** — legislative_items/events + status views + ingest + query | 15 | `4a2b50e` (stale) | `sql_store.py`, `schema.sql`, `query/classifier.py` | `migrate_2026_07_29_legislative_status.sql` |
| 3 | **`worktree-committee-count-investigation`** — item-number normalization | 1 | `42d4db7` (current main) | `sql_store.py` | `migrate_2026_07_29_normalize_item_numbers.sql` |
| 4 | **`worktree-goal-trackability`** — goals-tab polish | 15 (incl. inherited schema) | **stacked on `dev`@`3593938`** | inherits `dev`'s schema files + goals | (inherits `dev`'s) |
| 5 | **`ytd-reporting`** — cross-quarter YTD report | 8 | `4a2b50e` (stale) | `dashboard/aggregator.py`, `ytd.py` | `migrate_2026_07_29_ytd_indexes.sql` |
| 6 | **`worktree-grant-dedupe`** — canonical grant_key dedupe | 9 | `42d4db7` (current main) | `dashboard/aggregator.py` | — |
| 7 | **UNCOMMITTED** in the `dev` working tree — looks like "next-quarter questions" | uncommitted | — | `app.py`, `src/dashboard/review_questions.py`, `templates/redesign.html`, 2 dashboard tests, `.env.example` | — |

### Conflict map (who fights whom)
- **`sql_store.py`:** streams 1, 2, 3, 4 — real conflicts (esp. 1↔2).
- **`schema.sql`:** streams 1, 2, 4 — real conflicts (1↔2 mechanical, see §4).
- **`pipeline.py`:** 1, 4 — 4 just inherits 1's change; not a real conflict once 1 lands.
- **`dashboard/aggregator.py`:** 5 ↔ 6 — conflict with each other; neither touches schema.
- **Dependency:** 4 (`goal-trackability`) is built on top of 1 (`dev`) — it inherits `dev`'s schema **including the unfixed C1 bug**.

### Two structural facts that shape the whole plan
1. **Stale base.** Streams 1, 2, 4, 5 branch from `4a2b50e`, which predates several migrations already merged to `origin/main` (`migrate_2026_07_10_projects`, `2026_07_14_type_proposals`, `2026_07_14_vacancy_source_file`, `2026_07_15_registry_description`). After rebasing onto current `main`, **re-check for redundant/duplicate changes** — notably my Task 5 add of `vacancies.source_file` almost certainly duplicates the already-merged `migrate_2026_07_14_vacancy_source_file.sql` (idempotent, harmless, but drop the dup to keep it clean).
2. **`dev` is not a clean feature branch.** It carries a stray `145bd81` (YTD design-spec doc committed by another session) and the working tree holds stream #7's uncommitted files. Isolate the schema work onto a clean branch before PR (§2, P1).

---

## 1. Blockers & decisions (resolve before executing)

- **B1 — C1 (my schema): re-ingest FK violation.** The 5 legislative surrogate FKs (`votes.resolution_id/meeting_id`, `meeting_actions.meeting_id/resolution_id/legislation_id`) have no `ON DELETE`, so the pre-existing re-ingest cleanup (`sql_store.py` `delete_structured_rows`) violates them. **Fix before stream 1 lands** (add `ON DELETE SET NULL` — this also matches stream 2's `legislative_item_id` pattern). `goal-trackability` inherits this until then.
- **B2 — Which streams are actually done?** I cannot see the other tabs. `goal-trackability` was committed minutes ago (likely still live). **Confirm each stream is finished before merging it** — do not integrate a branch that's mid-edit elsewhere.
- **B3 — Stream #7 is uncommitted** in the shared `dev` working tree. Commit it to its own branch (or stash) before any rebase of `dev`, or it will get tangled into another PR.

---

## 2. Prerequisites (do once)

```bash
# P0 — refresh the target
git fetch origin
git checkout main && git merge --ff-only origin/main      # local main == origin/main (42d4db7)

# P1 — isolate the schema work off dev onto a clean feature branch
#      (dev carries a stray YTD-spec commit + stream #7's uncommitted files)
git checkout -b feat/schema-relationships dev              # start from dev's tip (359f449)
#   then drop the stray YTD-spec doc commit if it rode along:
#   git rebase --onto <its-parent> 145bd81 feat/schema-relationships   # (or cherry-pick only the schema commits onto a fresh branch off main)

# P3 — get stream #7 out of the shared working tree first
git stash push -m "next-quarter-questions WIP" -- app.py src/dashboard/review_questions.py \
    templates/redesign.html tests/dashboard/test_questions_route.py tests/dashboard/test_review_questions.py .env.example
#   (or: commit them to a branch `feat/next-quarter-questions` — coordinate with whoever owns that tab)
```

> If cherry-picking is cleaner than rebasing away the stray commit: create `feat/schema-relationships` fresh from `main`, then `git cherry-pick` the 12 schema commits (`53d9cbb`, `c39766a`, `9ea6df9`, `3f50ec8`, `4c91ccd`, `5926c91`, `328216b`, `3593938`, `b02dae9`, `cf9c806`, `6bb7d2b`, `359f449`) — skipping `145bd81`.

---

## 3. Ordered integration

Foundational schema first (so schema-touching branches rebase onto it), dashboard-only last. **Each stream: rebase onto current `main` → reconcile → run its tests → PR → (after merge) apply its migration.**

### Step 1 — `feat/schema-relationships` (stream 1) — the foundation
1. Fix **B1/C1** + the fix-soon cluster (document_chunks dept FK on fresh; `canonical_name` length guard; `merge_*` dup==keep guard; test singleton mutation; add `addopts = -m "not integration"` to `pytest.ini`).
2. Rebase onto `main`; reconcile the stale-base duplicates (drop the redundant `vacancies.source_file` add if `migrate_2026_07_14_vacancy_source_file` is already on main).
3. Run `pytest` (unit/fake-cursor suites must pass; the 3 live-DB `test_resolution_store.py` tests need the migration applied — expected).
4. PR → `main`. **After merge, apply the migration** (see §5).

### Step 2 — `legislative-status-tracking` (stream 2)
1. Confirm it's done (B2).
2. Rebase onto `main` (now includes stream 1). **Expected conflicts:** `sql/schema.sql` and `src/storage/sql_store.py`.
3. **Reconcile (see §4 — mechanical, keep both).** Its `legislation.legislative_item_id` and my `legislation.sponsor_id` are different columns → keep both `ADD COLUMN IF NOT EXISTS` lines. In `sql_store.py`, keep both my resolver/self-safe-insert changes and their legislative-item resolution + event emission.
4. Run its scratch-DB tests; PR → `main`; apply `migrate_2026_07_29_legislative_status.sql`.

### Step 3 — `committee-count-investigation` (stream 3, tiny — 1 commit)
1. Rebase onto `main`. **Expected conflict:** `sql_store.py` (small). Its item-number normalization may interact with my `meeting_actions.item_number`-based legislative backfill — verify normalization runs *before/consistently with* the FK backfill matching on `item_number`.
2. PR → `main`; apply `migrate_2026_07_29_normalize_item_numbers.sql`.

### Step 4 — `goal-trackability` (stream 4)
1. Confirm done (B2 — it was just edited).
2. Rebase onto `main`. Because it was stacked on `dev`, **its inherited schema commits collapse to no-ops** after rebase (they're already in `main` via stream 1) — only the goals-tab polish remains. Resolve any residual `sql_store.py`/`pipeline.py`/`schema.sql` markers by taking `main`'s side for the schema and keeping only its goals changes.
3. PR → `main` (no new migration).

### Step 5 — `ytd-reporting` (stream 5)
1. Rebase onto `main`. No schema-file conflict with 1–4 (touches `aggregator.py`, `ytd.py`). PR → `main`; apply `migrate_2026_07_29_ytd_indexes.sql`.

### Step 6 — `grant-dedupe` (stream 6)
1. Rebase onto `main`. **Expected conflict with stream 5** on `dashboard/aggregator.py` (both edit it). Resolve by hand; PR → `main` (no migration).

> Streams 5 and 6 are independent of the schema work — if they're done, they can go **first/in parallel** with 1–4; only sequence them relative to *each other* (aggregator.py). Order above is just "schema before dashboard" for a clean base.

---

## 4. The legislation reconciliation (mechanical — keep both)

Verified: the two legislation efforts are **complementary, not competing**.

- **Stream 1 (mine)** adds to `legislation`: `sponsor_id → council_members(id)`; and `meeting_actions.legislation_id → legislation(id)`. Entity/provenance links.
- **Stream 2 (theirs)** adds NEW tables `legislative_items`, `legislative_events`; and `legislation.legislative_item_id → legislative_items(id) ON DELETE SET NULL`; plus derived status views. Lifecycle tracking.

They touch the **same `legislation` table but different columns**, both via `ADD COLUMN IF NOT EXISTS`. Resolution when rebasing stream 2:
- `schema.sql` / migration: keep **both** blocks; order doesn't matter (idempotent).
- `sql_store.py`: keep **both** sets of methods (my resolvers + self-safe inserts; their `resolve legislative items` + `emit lifecycle events at ingest`). Watch the ingest path in `insert_legislation_rows`/`insert_meeting_action_rows` — both sessions may add logic there; merge, don't overwrite.
- Align FK delete-policy: my C1 fix makes the 5 surrogate FKs `ON DELETE SET NULL`, matching their `legislative_item_id` — consistent, no further reconciliation.

---

## 5. Migration apply order (after all merges, against the live DB)

Migrations are date-stamped and mostly idempotent, but **apply in this order**, on a Supabase **branch first**, then production (per the schema verification runbook `docs/superpowers/plans/2026-07-28-schema-relationships-VERIFICATION.md`):

1. *(already on `origin/main`)* `2026_07_10_projects`, `2026_07_14_type_proposals`, `2026_07_14_vacancy_source_file`, `2026_07_15_registry_description`
2. **`2026_07_28_schema_relationships`** (stream 1) — apply **after the app has initialized** (so `document_chunks` exists for its guarded FKs) → run `scripts/backfill_entities_2026_07_28.py` → apply `sql/seed_harrisburg_department_aliases.sql` → re-run the backfill. (Full detail in the schema VERIFICATION runbook.)
3. **`2026_07_29_legislative_status`** (stream 2) + its status backfill script
4. **`2026_07_29_ytd_indexes`** (stream 5)
5. **`2026_07_29_normalize_item_numbers`** (stream 3)

Re-check after each: my/others' stale-base additions may duplicate something already on main (idempotent, but verify counts).

---

## 6. What I need from you to move past "plan" → "execute"

1. **Which streams are DONE** (per B2) — I'll only integrate finished ones.
2. Go-ahead to **fix C1** on the schema branch (the one change gating stream 1).
3. Whether you want **me to drive** the rebases/PRs for the streams you confirm are done, or hand this runbook to each tab.
4. Ownership of **stream #7** (the uncommitted next-quarter-questions work) — commit to its own branch, or is that another tab's job?
