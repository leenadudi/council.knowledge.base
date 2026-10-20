# Projects Grouping — Design

**Date:** 2026-07-10
**Status:** Proposed

## Problem

In the Projects tab, one real-world initiative often appears as several near-identical
cards. Example (department "Mayor / Administration"): five separate **Contract** cards, all
"Lamar Advantage GP Company, LLC" billboard leases (Res 10–14), differing only by location.
They read as one thing and clutter the view.

These five are **parallel siblings** (five distinct lease agreements with the same vendor),
not sequential stages — so the fix is **grouping**, not a timeline. A lifecycle **timeline**
(one initiative moving through stages across resolutions/meetings/payments) is a genuinely
useful but **separate** feature, deferred to a follow-on design.

## Goal

Collapse sibling project cards into one expandable group so a program like the Lamar
billboard leases shows as a single "Lamar … — 5 agreements" card that expands to its members.

## Non-goals

- No lifecycle timeline this phase (separate design).
- No LLM use — the Projects layer is deterministic; grouping stays deterministic and free.
- No grouping of grants (each grant award is already one item).
- No change to filters, KPI counts, or search semantics.

## Approach

Grouping is a **display** concern. Rather than change `Projects.build()`'s return contract
(which the front end's filters, counts, and search all read as a flat `projects` list), the
backend **tags each project with a deterministic `group_key`**, and the front end clusters by
that key at render time. Smaller blast radius; the flat list stays intact for everything else.

### Grouping key

Two projects are the same group when they share **normalized vendor + type**, within a
department (the Projects list already sections by department, so the department is implicit in
the render). Chosen over vendor-only (over-merges unrelated deals) and vendor+subject-keywords
(brittle on wording).

- Applies to **resolution-derived** projects that have a vendor (`party`): `contract`,
  `grant_action`, `land_development`, `other`.
- Grants and administrative items get `group_key = None` (never grouped).

### Backend — `src/dashboard/projects.py`

- Add `_normalize_party(vendor: str) -> str`: lowercase, strip, collapse whitespace, drop
  trailing corporate suffixes/punctuation (", LLC", ", Inc.", ".") so
  "Lamar Advantage GP Company, LLC" variants match. Reuse the spirit of existing normalization.
- In `build()`, when assembling each resolution-derived project `rec`, set:
  - `rec["group_key"]` = `f"{_normalize_party(party)}|{typ}"` when `party` is truthy, else `None`.
  - grant projects and administrative items: `rec["group_key"] = None`.
- No change to `projects` / `administrative` / `counts` / `funding_in_flight` shapes; only a new
  per-item field is added.

### Front end — `templates/redesign.html` (`renderProjects`, `projRow`, `openProject`)

- KPIs, filters (type/dept/status), search, and `_projIndex` continue to operate on the flat
  `all` list — **unchanged**.
- In the per-department render block (the `depVal==='All'` branch and the single-department
  branch), before emitting rows: within the department's `items`, bucket by `group_key`
  (items with `group_key === null`, or a key with only one member, are singletons).
  - **Singleton** → render `projRow(p)` exactly as today.
  - **Group (≥2 members, same non-null key)** → render a collapsed **group card**:
    - vendor (from `party`) as title, a type badge with count ("Contract ×5"),
    - **summed amount** across members (omit if all null),
    - **rolled-up status**: "Active" if any member Active, else the most common member status;
      show a "Needs attention" flag if any member has `attention`.
    - a chevron; clicking toggles an expanded region containing each member's `projRow(p)`
      (existing per-row click → `openProject(id)` still opens the individual dossier).
- Group expand/collapse is local UI state keyed by `group_key`; default collapsed.
- Administrative section: unchanged (rendered flat).

## Data flow

```
build() (backend)
  each resolution-derived project gets group_key = normalize(vendor)|type   (grants: null)
        |
        v
renderProjects() (frontend)
  per department: bucket items by group_key
     - 1 member / null  -> projRow (today's card)
     - >=2 members      -> collapsed group card [Vendor · Type xN · $sum · status]
                              expand -> member projRows (each opens its own dossier)
```

## Testing

- Python unit tests (`tests/dashboard/test_projects_grouping.py`):
  - `_normalize_party` collapses "Lamar Advantage GP Company, LLC" / "LAMAR ADVANTAGE GP COMPANY LLC"
    to the same value.
  - `build()` assigns the Lamar contract rows an identical `group_key`; a Lamar `grant_action`
    (if present) gets a different key; a different vendor differs; grants get `group_key = None`.
- Front-end behavior verified by running the app (no JS test harness in this repo): the Lamar 5
  render as one expandable "Lamar … Contract ×5" card that expands to five member rows, each of
  which still opens its dossier; singletons and the Administrative section look unchanged.

## Rollout

Pure code + Python tests. No migration, no LLM, no data change. Verify live in the running app.
```
