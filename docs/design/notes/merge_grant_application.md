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

An intelligent knowledge base that helps a city council and clerk govern faster — instant answers from records they'd otherwise dig through by hand.

---

## How much money are you requesting?

**$466.39**

---

## Budget Breakdown

Every line is a real, current rate, funding roughly six months of running ClerkFlow while I finish the tracking features. Hosting the databases — not the AI — is the biggest cost. (AI lines are usage estimates at published token rates; actual spend is metered per call in the app.)

- **Databases: $347.10**
  - Supabase Pro — Postgres + pgvector, 8 GB disk (extracted data + vector index): $25.00/mo × 6 months = $150.00
  - Neo4j AuraDB — free tier, used during development: $0.00
  - Neo4j AuraDB Professional — 1 GB graph instance @ $0.09/hr, live pilot only: $65.70/mo × 3 months = $197.10
- **AI / Model APIs: $104.30**
  - Answer synthesis — Claude Sonnet 4.6 ($3 / $15 per M tok), ~2,000 queries (dev + pilot), prompt caching on: $56.40
  - Structured extraction at ingest (records → SQL + graph) — Claude Haiku 4.5 ($1 / $5 per M tok), ~4,000 chunks: $20.00
  - Document parsing / OCR fallback — Claude Sonnet 4.6 Vision, ~500 scanned / slide-deck page-passes: $15.00
  - Self-scoring eval judge — Claude Haiku 4.5, ~1,200 sampled eval runs: $7.50
  - Query routing / classifier — Claude Haiku 4.5, ~2,000 queries: $4.80
  - Embeddings — OpenAI text-embedding-3-small ($0.02 per M tok), corpus + re-ingests: $0.60
- **Hosting & Domain: $14.99**
  - Domain — clerkflow.org, .org 1-yr registration (Namecheap): $14.99
  - Web app hosting — Vercel Hobby tier: $0.00
  - SSL certificate — Let's Encrypt (auto-provisioned via Vercel): $0.00
  - Source + CI — GitHub Free: $0.00

**Total: $466.39**

---

## Share what you're working on

I grew up in a town in Kansas where AI was barely part of the conversation. Then I got to MIT, and the gap hit me immediately — the tools and fluency my classmates took for granted were things people back home had hardly heard of. That contrast is what I can't stop thinking about: the places that could benefit most from AI are often the ones least set up to adopt it. I wanted to work on that gap — to bring real AI solutions to the people and institutions that aren't predisposed to use them.

So I started asking around, and one conversation stuck with me more than any other. The city clerk of Harrisburg, Pennsylvania told me that city councils have essentially all of their data locked in PDFs — budgets, resolutions, meeting minutes, vote records — and that anyone who needs an answer has to go in by hand, pull it out, and build their plans from scratch. Local government barely uses modern technology. In some cases councils had gone as far as restricting AI outright — not out of principle, but because members didn't know how to use it safely or well. Here was a place drowning in its own documents, and the one tool that could help had been ruled out because no one had built it *for them*.

That's why I'm building ClerkFlow: an intelligent knowledge base for city government, made for the people who actually run it. A clerk shouldn't have to spend a weekend digging through PDFs to answer something as basic as "did we already authorize this?" or "which grants are still active?" ClerkFlow reads a city's raw records and builds a structured understanding of how that government actually works — it extracts the people, departments, resolutions, vendors, grants, dollar amounts, and votes out of every file and assembles them into a knowledge graph, keeps the numbers in a structured database, and leaves the full text semantically searchable. Ask it something and it reasons across all three at once: the graph resolves relationships, the database does the math, and retrieval grounds every answer in the source. Plain-English questions are just the front door.

That structure is what turns hours of work into seconds. Because ClerkFlow knows *who authorized what* and *who voted how*, a council can instantly reconcile what they approved against what the city actually spent and catch overspending that was never authorized, track grants across fiscal years — active status, matching funds, rollover — and line each department's stated goals up against what it delivered. Getting messy government PDFs to parse cleanly — scanned pages, white-on-black slide decks — was genuinely hard, so I built an ingestion pipeline with an OCR fallback, and the system scores its own answers so I know when it's wrong instead of just confident. It even closes a loop: the gaps it surfaces become sharper questions the clerk can put into the *next* quarter's reports, so it gets better every cycle.

I'll be honest about where I am: the core works, I'm building it solo, and I've been paying for it out of pocket. `[Optional: one line of candor — something that's been hard, a mistake you made, or what you've learned building this. Winning apps do this well and it makes you human.]` This grant wouldn't fund a vision deck — it would keep the databases and APIs running so I can finish the features the clerk actually asked for, instead of throttling usage because the monthly bill is a student's problem.

And the dream is bigger than one clerk's office. There are nearly 20,000 municipal governments in the U.S., almost all running on the same PDFs and the same blind spots — and disproportionately the smaller towns, the ones like where I'm from, that never get built for. Once a city's records are legible to the people running it, they're legible to everyone. I want ClerkFlow to be the intelligence layer underneath local government: first making the people who run our cities faster and sharper, and ultimately making government legible by default. Harrisburg is where I prove it works, and building it in the open is how it reaches everywhere else — because bringing AI to the places it's been kept out of is exactly the problem I set out to solve.

*(Word count: ~600 / 1,000)*

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
