# Projects (Phase 1) — Design Spec

**Date:** 2026-07-09
**Status:** Approved (design), pending implementation plan
**Feature owner:** Council KB / ClerkFlow

## Problem

City Clerk Truesdale wants to see **how the city's projects are doing** — land
development plans, grant-funded initiatives, contracts, and "current activity in
certain areas." His notes: *"scrape, review and organize certain components of
resolutions; status on the resolution; active land development plans."*

Today this information is scattered: grants live in the Finances tab, resolutions
live in a separate dossier, and there is no single "project" concept tying them
together. A council member cannot answer "what's the status of the Pennmark land
development?" or "which grant-funded projects are active right now?" in one place.

## Insight

Projects already exist in the data — they are just fragmented across two tables:

- **`grants`** (121 rows) — nearly all named for real initiatives/locations
  (e.g. *TASA Grant (Walnut St. East-West Connection)*, *Courthouse Connection
  Grant*, CDBG, Lead Hazard Reduction). Each grant is a grant-funded project.
- **`resolutions`** (7 today, growing) — council authorizations, e.g. res 5-2026
  *"Preliminary/Final Land Development"* (Pennmark Harrisburg Holdings), res 9-2026
  USDOT $3M grant application, res 2-2026 a Parks landscape contract.

A **Project** is therefore a *synthesis* over these tables, not a new data silo.
Building it also reframes the grants view and "fixes" the resolutions surface.

## Scope (v1 — Phase 1)

**In scope**
- A live-derived Projects data layer assembled from `grants` + `resolutions`.
- Deterministic resolution classification (type) and status normalization.
- A new **Projects** tab: KPIs, filters (type/department/status/search), list, detail.
- All deterministic — zero LLM/ingestion cost; auto-updates as more resolutions
  are ingested (no re-materialization step).

**Out of scope (later phases)**
- Ingesting more resolutions / new land-development document types (Phase 2 —
  user is ingesting more resolutions separately; this layer picks them up live).
- Structured `area`/location extraction (v1 shows the title text, which contains it).
- Fuzzy auto-merging a grant with its authorizing resolution (kept separate in v1).
- Smarter "next-quarter questions" fed by projects (separate spec, paused, folds in
  after Projects ships — see docs/.../2026-07-09-next-quarter-questions-design.md).
- A materialized `projects` table (rejected: needs entity resolution, doesn't
  auto-update without ingest logic).

## Architecture

Approach A — **live aggregation** over existing tables, computed every build.

### 1. Data layer — `src/dashboard/projects.py`

A `Projects` builder (mirrors `DashboardAggregator` / `ReviewQuestions` style;
reuses `DashboardAggregator._dept_key` / `_dept_display` for canonical departments).
`build()` returns:

```
{
  "projects": [ { ...project... } ],       # initiative-type, sorted
  "administrative": [ { ...project... } ], # budget/appointment resolutions, bucketed
  "counts": { "active": N, "attention": N, "by_type": {...} },
  "funding_in_flight": <float>
}
```

Each **project** record:
```
{
  "id": "grant-<id>" | "res-<resolution_number>",
  "source": "grant" | "resolution",
  "type": "grant" | "land_development" | "grant_action" | "contract"
          | "budget" | "appointment" | "other",
  "title": <grant_name | resolution title>,
  "department": <canonical display name>,
  "party": <grant funder | resolution vendor/developer>,   # nullable
  "amount": <float|null>,
  "status": "Proposed" | "Active" | "Awarded" | "Completed" | "Closed" | "Stalled",
  "date": <ISO date|null>,          # grant start_date or resolution adopted_date
  "end_date": <ISO date|null>,      # grants only
  "source_file": <str|null>,
  "resolution_number": <str|null>
}
```

Wired into `DashboardAggregator.build()` as a new `projects` panel via `_safe()`.

### 2. Resolution classification (deterministic, free)

Keyword rules on the resolution title assign `type` (first match wins):
- `land_development` — /land development|subdivision|zoning|plat|rezon/
- `grant_action` — /grant/
- `contract` — /agreement|contract|professional services|purchase|negotiat|lease/
- `budget` — /budget|appropriat|tax|millage/
- `appointment` — /appoint|reappoint|resign|confirm/
- `other` — anything else

`budget` and `appointment` route to `administrative`; all other types are headline
projects. Grants always have `type = "grant"`.

### 3. Status normalization

Map source statuses to a common project lifecycle:
- Grants: `active`→Active, `awarded`→Awarded, `applied`/`pending`→Proposed,
  `closed`→Closed, else→Active.
- Resolutions: `passed`/`adopted`/`approved`→Active, `tabled`→Stalled,
  `failed`/`defeated`→Closed, else→Proposed.

"Needs attention" (for the KPI) = a grant-type project expiring within 120 days,
or any project with status Stalled.

### 4. Frontend — Projects tab

- New nav item **"Projects"** under Explore (data view).
- `renderProjects()` reads `D.projects`.
- **KPIs:** active projects, funding in flight, needs-attention count, count by type.
- **Filters:** type, department, status, free-text search (reuse the goals/finances
  filter patterns + `dkey`/`deptDisplay`).
- **List:** card/row per project — title, type tag (color-coded), department, party,
  amount, status pill, date.
- **Detail:** click → slide-over (reuse the existing dossier `.sheet` pattern) showing
  the source document, party, amount, status, and — for resolutions — the resolution
  number; for grants, the award window.
- **Administrative** resolutions shown in a collapsed section, not headline projects.
- Live: the list grows automatically as grants/resolutions are ingested (served from
  the 90s-cached `/dashboard/data`).

### 5. Relationship to existing tabs

- **Finances** keeps the money lens (grant dollars, budget vs actual) — unchanged.
- **Projects** is the initiative lens and the primary resolutions surface.
- Grants appear in both (funding in Finances, initiative in Projects) — intentional.

## Data flow

```
/dashboard/data (90s cache, FREE)
  └─ DashboardAggregator.build()
       └─ projects: Projects.build()   # live aggregation over grants + resolutions
            → D.projects in the browser → renderProjects()
```

No LLM. No ingestion step. New resolutions appear on the next cache refresh.

## Error handling

- `_safe()` isolates the panel — a failure never takes down the dashboard.
- Rows with a null/blank title are skipped (cannot form a meaningful project).
- Unknown source statuses fall back to the mapped default (Active for grants/passed
  resolutions, Proposed otherwise), never an error.

## Testing

- **Unit — classification:** each type rule (land_development, grant_action, contract,
  budget, appointment, other) from representative titles.
- **Unit — status normalization:** each source status → expected lifecycle value.
- **Unit — assembly:** grants + resolutions → typed projects; administrative bucketing;
  department-variant canonicalization; empty tables; null-title skip.
- **build() shape:** aggregator payload includes `projects` with the documented keys.
- **Frontend:** extract `<script>` + `node --check`; Jinja parse check after edits.

## Success criteria

- A Projects tab lists grant-funded and resolution-authorized initiatives together,
  each with a type, department, party, amount, and normalized status.
- Filtering by type surfaces "active land development plans" as its own group.
- The list updates automatically after more resolutions are ingested — no code change.
- The always-on dashboard build issues zero LLM calls.
