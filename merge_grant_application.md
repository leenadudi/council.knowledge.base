# Merge Grant Application — Draft

> Personal/contact and financial-need fields are left as `[bracketed placeholders]` for you to fill in truthfully. Everything about the project is drafted and ready.

---

## First Name
`[Leena]`

## Last Name
`[Dudi]`

## Email
`[your preferred email — likely your school/personal address, not your work one]`

## Phone number
`[your phone number]`

## City / Country
`[e.g., Cambridge, MA, USA]`

## LinkedIn / Twitter / Website
`[best link that represents you and your work — GitHub, personal site, or LinkedIn]`

---

## One-liner for the project

An open-source AI knowledge base that lets a city council, its clerk, and residents ask plain-English questions about their local government — budgets, grants, resolutions, and votes — and get answers cited straight from the source documents.

---

## How much money are you requesting?

**$600**

---

## Budget Breakdown

Everything below is a real, recurring cost of running the system. The grant would fund roughly 5–6 months of operation while I keep building. Note that hosting — not the API — is the largest ongoing cost.

| Line item | Amount | What it covers |
|---|---|---|
| LLM API credits (Anthropic Claude) | $200 | Answer synthesis, Vision-model OCR fallback for scanned/slide PDFs, the automated evaluation judge, and the tailored-question generator. Usage is already metered in the app, so spend is tracked per call. |
| Embedding API credits | $25 | Vectorizing every document chunk at ingestion so questions can be matched to the right passages (largely a one-time cost per document). |
| Managed Postgres + pgvector (Supabase) | $150 | The primary database: extracted budget/grant/resolution/vote data **and** the vector search index live here. ~6 months at the paid tier. |
| Managed graph database (Neo4j Aura) | $165 | The relationship layer — who directs which department, which resolution authorized which vendor, how each council member voted. |
| Domain + deployment (Vercel) | $60 | Hosting the public dashboard and query interface, plus the domain. |
| **Total** | **$600** | |

*(Roughly two-thirds of this is database + hosting; API usage is the smaller share and is metered per call inside the app.)*

---

## Share what you're working on

I'm building a knowledge base for the City of Harrisburg's government — a tool that turns thousands of pages of dense civic documents into something a council member, the city clerk, or an ordinary resident can actually *ask questions of*.

Local government runs on paperwork: quarterly department reports, budgets, council resolutions, meeting minutes, legislation, grant records. The information people need to hold a city accountable technically exists — it's just buried across hundreds of PDFs that almost no one has the time to read. "Did we spend more than the council authorized this year?" "Which grants are still active, and did the matching funds roll forward?" "What did the Public Works department promise last quarter, and did they deliver?" These are answerable questions today only if you're willing to dig through a filing cabinet's worth of documents. My system answers them in seconds, with a citation back to the exact source.

Under the hood it's a retrieval system built on three coordinated databases. A vector store handles semantic search over document text. A SQL database holds the structured numbers I extract — expenditures, metrics, grants, vacancies, resolutions, votes, appropriations. A graph database captures relationships: which person directs which department, which resolution awarded a contract to which vendor, how each council member voted. When a question comes in, a classifier decides which stores to hit, pulls the evidence, and an LLM writes a plain-English answer with citations. Getting messy government PDFs — including white-on-black slide decks and scanned pages — to parse cleanly meant building a multi-parser ingestion pipeline with an OCR fallback, and an evaluation suite so I can measure whether answers are actually correct rather than just plausible.

What makes this compelling to me is that it isn't hypothetical. I sat down with Harrisburg's City Clerk, and he walked me through what he actually needs: not just a Q&A box, but *tracking over time* and a *dashboard* — a way to surface spending that council never authorized, follow grants and their matching funds, watch departmental goals across quarters, and even generate sharper questions to put into the next quarter's reports. Harrisburg runs a strong-mayor system where spending authority sits with the council, so "authorized vs. unauthorized spending" isn't a gimmick — it's a real accountability mechanism that no one currently has the tooling to check. That conversation reshaped my roadmap, and I've already shipped the first pieces of the dashboard.

I started this because I care about the unglamorous infrastructure of democracy. Transparency portals usually stop at *dumping* documents online; almost none help you *understand* them. I wanted to close that gap for one real city, with one real user who needs it, and build it in the open so other municipalities can adopt the same approach. It's a genuinely hard technical problem — reconciling numbers across inconsistent documents, attributing actions to the right branch of government, keeping answers grounded and citable — and it's one where getting it right actually helps people.

The core system works. The grant would let me keep the databases and APIs running and finish the tracking-and-dashboard features the clerk asked for, rather than shutting things down because the monthly cloud bill is coming out of a student's pocket.

*(Word count: ~560 / 1,000)*

---

## Financial Need Qualification

> Draft framework below — please replace the brackets with your real situation. Keep it honest; the program explicitly reviews for genuine need.

**1. Current financial situation, challenges, and how aid would help.**

I'm a full-time student, and this project has been entirely self-funded so far. The recurring costs — LLM and embedding API usage, managed Postgres, a hosted graph database, and deployment — run to roughly `[$__]` per month, which I've been covering out of `[personal savings / a limited stipend / part-time income]`. `[Describe any specific challenge or unexpected expense here — e.g., tuition/rent pressure, a rise in usage costs as the document corpus grew, etc.]` I've built the system as far as I can before cost became the limiting factor: the core is working and I did the engineering myself, but every additional document I ingest and every question answered adds to the bill. A Merge Grant would cover the cloud and API costs for roughly 5–6 months, letting me finish the accountability and dashboard features instead of throttling usage or taking the system offline.

**2. Anything about your personal or family situation worth knowing.**

`[Optional — share anything relevant here, or write "Nothing additional." Only include what you're comfortable sharing.]`

**3. If applying as a student / living at home: alternatives if you didn't get the grant.**

If I couldn't secure the Merge Grant, I would `[describe your realistic alternatives — e.g., "sharply limit API usage and pause new document ingestion," "move services to free tiers with tighter limits," "continue funding it slowly out of savings," etc.]`. `[Note the role, if any, family support would or wouldn't play.]` Realistically, without funding the project would `[slow significantly / stall / continue at a reduced pace]`, since the ongoing cloud costs are the primary barrier — not the work itself.

---

## Pre-filled fields (confirm these match your situation)

- **Employment status:** Student, Full-Time
- **Dependents:** None
- **Household income:** Dual
- **Individuals in home:** 3+
