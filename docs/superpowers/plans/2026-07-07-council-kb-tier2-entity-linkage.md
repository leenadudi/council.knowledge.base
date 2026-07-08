# Council KB — Tier 2: Entity Linkage (design spec)

> **Status: design-level, not yet bite-sized.** This picks an approach and scopes the features. Once you sign off on the approach, I expand it into a TDD task-by-task plan like the Tier 1 plan.

**Goal:** Stop showing tables in isolation. Connect resolution ↔ its votes ↔ vendor ↔ adopting meeting ↔ department budget/goals, so a council member can pull one thread and see the whole decision.

## The approach decision: SQL-first, graph later

We have two linkage layers already populated at ingest:
- **Postgres** — shared keys: `votes.resolution_number` = `resolutions.resolution_number`; `resolutions.adopted_date` ≈ `meetings.meeting_date`; department across `expenditures`/`grants`/`goals` (via `_dept_key`). Always up. Unit-testable with `_FakeStore`.
- **Neo4j** — already models the entity web (`Resolution-[:AWARDS_CONTRACT_TO]->Vendor`, `CouncilMember-[:VOTED]->Resolution`, `Department-[:REPORTED_IN]->Document`, `Person-[:DIRECTS/MANAGES]->Department`) but is **optional** — the app boots with it down, and it's currently unused except staff lookups.

**Recommendation: build linkage in SQL first.** It's always available, testable without a live graph, and every join we need already has a key or a clean date match. Reserve Neo4j for the one thing SQL is bad at — open-ended multi-hop traversal ("everything connected to vendor X within 2 hops") — as a later, optional "explore" view. This matches "iterate and test quickly": no Neo4j dependency in the core path.

## Features (in priority order)

### 1. Resolution dossier (the centerpiece)
Click any resolution → a slide-over showing: the resolution (title/amount/**status** — passed/failed/tabled), the **vendor** it awards, the **meeting** it was adopted in (`adopted_date` = `meeting_date`), and the **department's** budget/goal context. All SQL joins on existing keys.

**No roll-call in the dossier.** Per repeated product guidance, what matters is *whether it passed* (the `status` field), not how each member voted — Harrisburg's votes are near-unanimous, so the tally is low signal. `votes` stays backend-only; the dossier surfaces status, not a vote breakdown.

- New: `DashboardAggregator.build_resolution_dossier(resolution_number)` → dict bundling the joins.
- New route: `GET /dashboard/resolution/<rn>` (read-only, cached).
- Frontend: make resolution rows in Decisions/roll-call clickable → dossier view.

### 2. resolution ↔ meeting (folded from Tier 1)
Enrich `_build_resolutions` with `LEFT JOIN meetings ON resolutions.adopted_date = meetings.meeting_date` → add `meeting_session`, `meeting_source`. Lets Decisions show "adopted at the Feb 10 Legislative Session." (Requires updating the 2 existing `_build_resolutions` tests.)

### 3. Member voting affinity
"Who votes together?" From `votes`, pairwise agreement rate per member pair on shared resolutions. Pure SQL/Python aggregation. Powers a small "voting blocs" panel on the Voting Records view.

### 4. Provenance links
Every structured row has `source_chunk_id` / `source_file`. Surface "view source document" on dossiers and tables so a clerk can jump from a datapoint to the PDF it came from. (Reuses the existing `/documents` list; no new extraction.)

### 5. Graph explorer (optional, last)
IF Neo4j is up: a relationship view over the existing graph — "show everything linked to {vendor / department / member}" via `execute_cypher`. Gated behind a graph-available check; degrades to "unavailable" if the driver is down. This is the only piece that needs Neo4j.

## Phased tasks (expand to bite-sized on approval)

- **Phase 1** — `build_resolution_dossier()` + tests (SQL joins: resolution + votes + vendor + meeting + dept budget). Backend only.
- **Phase 2** — `/dashboard/resolution/<rn>` route + dossier frontend view; make resolution rows clickable.
- **Phase 3** — resolution↔meeting enrichment in `_build_resolutions` (+ test updates) + show session in Decisions.
- **Phase 4** — member voting affinity aggregation + "voting blocs" panel.
- **Phase 5** — provenance "view source" links across dossier + tables.
- **Phase 6 (optional)** — Neo4j graph-explorer view, guarded by availability.

## Constraints (same as Tier 1)
- No LLM / ingestion spend — all SQL + presentation.
- Neo4j is optional: nothing in Phases 1–5 may depend on it; Phase 6 must degrade gracefully.
- Reuse `_dept_key`, `_FakeStore` test pattern, Decimal→float coercion, existing design system.

## Decided
Dossier renders as a **slide-over panel** (inline, keeps Decisions-table context) for v1 — decided 2026-07-07. A full briefing page can come later if needed.
