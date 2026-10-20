# Harrisburg City Clerk Meeting — Build Notes

**Date:** 2026-06-29
**Source:** Meeting with the Harrisburg City Clerk
**Status:** Raw synthesis — to be triaged into specs/plans

---

## The big shift in what's being asked

Today the system is a **Q&A engine**: ask a question, get a cited answer from quarterly reports.

The clerk is asking for two things on top of that:

1. **Tracking** — follow specific things *over time* (projects, grants, spending, goals, board terms), not just answer one-off questions.
2. **A dashboard** — a visual surface so council members and staff can *see* the state of things and make better decisions, without typing a query.

A lot of what he wants depends on data that **does not live in quarterly reports** — it lives in **resolutions** (council authorizations), **budget documents**, and **grant award letters**. Our own spec parks these in Phase 7+. So several of these asks are blocked on ingesting resolutions/budget data first. That's the single most important sequencing fact below.

---

## Domain context he gave us (this shapes the data model)

Harrisburg is **strong-mayor + city council**, and **council (legislative) is deliberately separate from the administration (executive).** This matters because:

- **Spending authority sits with council.** Any spending must be **approved by council** — i.e. tied to a **resolution**. This is the backbone of the "authorized vs. unauthorized spending" feature.
- We need to attribute actions to the right branch: a department *requests*, council *authorizes*. Don't model these as the same actor.

**Council term / election structure (for time-scoping and attribution):**
- **Council president & vice president:** elected every **2 years** (the "legislative cycle").
- **Council seats:** staggered — **3 seats elected one cycle, 2 seats the other cycle** (4-year terms, staggered every 2 years).
- Implication: when we attribute a vote, a goal, or an authorization, we need to know **which council composition / which president** was in office at that time. Add effective-dated terms, not just a flat "council member" list.

---

## Build themes (each is a candidate work item)

### 1. Authorized vs. unauthorized spending  ⭐ highest-signal, most concrete
**What he wants:** Year-to-date spending tracked against what council actually approved. Surface **overspending that was never authorized** by council.

**What it needs:**
- Ingest **resolutions** (authorization + dollar amount + department) — this is spec Phase 7.
- Ingest / extract **YTD spend** per department/account (already partly in quarterly budget tables → SQL `expenditures`).
- A reconciliation: `authorized_amount` (from resolutions) vs. `ytd_spend` (from reports/budget) → flag where spend > authorized or has no matching authorization.

**Open Q:** Where does the authoritative YTD spend number come from — quarterly report budget tables, or a separate finance/budget export? This determines accuracy.

---

### 2. Grant lifecycle tracking
**What he wants:** "If we knew we had this many grants active, and the matches — has the grant money been rolled forward to the next year?"

**Three distinct things to track per grant:**
- **Active status** — which grants are currently live.
- **Matching funds** — the local match required/committed for each grant.
- **Rollover** — whether unspent grant money carried into the next fiscal year.

**What it needs:** Extend the existing `grants` SQL table + `Grant` graph node with: `status`, `match_amount`, `fiscal_year`, `rolled_forward_amount`/`rollover_to_fy`. Grant award letters (spec Phase 8) give the cleanest source; quarterly "Grant Update" sections are a partial source we already parse.

---

### 3. Project & development tracking
**What he wants:** Timeline tracking for projects; development plans with **builder** and **type of project**; board appointments.

**Sub-items:**
- **Project timelines** — start/milestone/end dates per project, status over time.
- **Development projects** — attributes: builder/developer, project type, location, status.
- **Board appointments** — who is appointed to which board, term dates (ties into the term/election structure above).

**What it needs:** Project node already exists in the graph. Add timeline/milestone fields; add a `Developer`/`Builder` node and a `Board`/`Appointment` concept. Board appointments likely come from resolutions/minutes, not quarterly reports.

---

### 4. Goal tracking across quarters
**What he wants:** Department goals stated in quarterly reports (e.g. a "public works goal") should be **captured, shown on the dashboard, and re-citable in the next quarterly report.** Track goals and put forward **measures** so council can answer progress questions.

**Why this is high-value and low-blocker:** quarterly reports *already* contain an "Annual Goals" section we parse (spec §2.2, chunk type "Annual Goal Updates"). This is buildable on data we already have.

**What it needs:**
- Persist goals as first-class records (department, goal text, year, status/progress) instead of just free-text chunks.
- Link each goal to its progress updates across Q1→Q4 so you can show a trend and quote last quarter's stated goal in the next report.
- Optionally attach **measures/metrics** to each goal (e.g. tonnage, work orders) so progress is quantifiable.

---

### 5. The dashboard  ⭐ the thing he can actually *see*
**What he wants:** A visual surface for all the above — spending status, active grants + matches + rollover, project timelines, stated goals & progress. "Visual aid… help them understand."

**What it needs:** A read layer over the SQL/graph data feeding a web UI (we already have `app.py` + `templates/`). Each theme above becomes a dashboard panel. Build the dashboard *incrementally* — one panel per theme as that theme's data lands. Don't wait for all data to ship a first panel (Goals, theme 4, is the easiest first panel).

---

### 6. Tailored questions for quarterly reports  ⭐ this is the clerk's own workflow
**What he wants:** Use the tracked data to **generate more tailored questions** that get put *into* the quarterly report questionnaire each cycle — questions aligned to **individual council members' interests** — so departments report exactly what council needs and council makes better-informed decisions. He sees this as *his* tool for tracking and steering the effort.

**What it needs:**
- A notion of **council member interests/topics**.
- A generator that, given tracked data + unanswered/under-reported areas + a council member's interests, proposes specific questions to add to the next quarterly report.
- A loop: questions → answered in next quarter's reports → ingested → tracked → refine questions. This closes the improvement loop the system was already designed around (eval/feedback).

**Open Q:** Is this AI-generated draft questions for *him* to review and curate, or a more automated pipeline? My read: draft-and-curate, with him in the loop.

---

## How these depend on each other (suggested sequence)

```
Already-have data                          Needs new ingestion (Phase 7+)
─────────────────                          ──────────────────────────────
[4] Goal tracking      ─┐                  [1] Authorized vs. unauthorized
[6] Tailored questions ─┤                      spending      (needs resolutions)
                        ├─► [5] Dashboard    [2] Grant lifecycle (needs award letters)
                        │   (one panel       [3] Project/board  (needs resolutions/
                        │    per theme)           minutes)
```

**Recommended order:**
1. **Goal tracking (#4)** — buildable on data we already parse; lowest risk; immediately useful to the clerk.
2. **First dashboard panel (#5)** — wrap goal tracking in a visual so he sees value fast.
3. **Resolutions ingestion (spec Phase 7)** — unlocks #1 and most of #3. This is the big enabler.
4. **Authorized vs. unauthorized spending (#1)** — the highest-signal accountability feature, once resolutions are in.
5. **Grant lifecycle (#2)** and **project/board tracking (#3)** — extend schemas + add dashboard panels.
6. **Tailored question generator (#6)** — best built last because it leans on everything tracked above, though a v0 keyed off goals could come earlier.

---

## Open questions to take back to the clerk

1. **Spending source of truth:** Are YTD spend figures reliable enough in the quarterly budget tables, or is there a separate finance/budget system/export we should pull from for the "unauthorized overspend" feature?
2. **Resolutions access:** Do we have the resolution documents (and the vote records) in a form we can ingest? Format? How far back?
3. **Council member interests:** How are member interests captured today — does he track them, or do we infer from past questions/votes?
4. **Dashboard audience & access:** Who logs in — just the clerk, all council, the public? Affects auth and what's shown.
5. **Board appointments source:** Where do appointments live — resolutions, minutes, a roster he maintains?
6. **Tailored questions = draft or automated:** Does he want AI-drafted questions he curates, or a more hands-off pipeline?

---

## Notes-to-self / architecture observations

- Spec **Phase 7 (Resolutions)** is the linchpin for half these asks. Prioritizing it changes the spec's "future work" framing — worth a spec changelog update.
- The dashboard is a **new surface** not in the current spec (spec is query-API-centric). Needs its own mini-spec: data contract, panels, refresh cadence.
- The term/election structure means we need **effective-dated council composition** — don't bolt this on later; design the resolutions/vote schema with it from the start.
