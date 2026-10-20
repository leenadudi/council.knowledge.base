# City Council Knowledge Base — Project Specification

**Project Name:** Harrisburg City Government Knowledge Base  
**Version:** 1.1  
**Status:** Draft  
**Last Updated:** June 2026  
**Changelog:** Added future document type roadmap (resolutions, ordinances, minutes). Scoped initial build to quarterly reports only.

---

## 1. Overview

> **Implementation Scope Notice**
> 
> This specification describes the full intended architecture including future document types (resolutions, ordinances, meeting minutes, contracts, and others). However, **the initial implementation in Claude Code is scoped to quarterly reports only.** All sections marked with 🔮 describe future work that is fully designed but not yet being built. The architecture is intentionally designed so that future document types can be added without restructuring what is built now.

### 1.1 Problem Statement

The City of Harrisburg produces quarterly reports, budget documents, meeting notes, and operational data across multiple departments. This information exists as a collection of PDFs and slide decks that are difficult to search, cross-reference, or query. Government employees currently have no way to ask a question and get a reliable, cited answer drawn from the full body of city documents.

### 1.2 Solution

A multi-store knowledge base that ingests all city government documents, extracts and stores their content across three purpose-built databases, and provides a natural language query interface that returns accurate, cited answers. The system improves automatically each quarter as new documents are ingested and feedback is collected on answer quality.

### 1.3 Core Capabilities

- Accept natural language questions from government employees
- Return accurate answers with citations to source documents
- Handle numeric/budget questions, narrative/policy questions, and organizational/relationship questions
- Ingest new quarterly documents automatically
- Track answer quality and improve over time

---

## 2. Document Corpus

### 2.1 Current Scope — Quarterly Reports Only

**The initial build handles quarterly reports exclusively.** These are the documents currently being implemented in Claude Code. All other document types listed in section 2.3 are designed and planned but not part of the current build.

Quarterly reports come from each city department and are produced four times per year. Based on the sample documents provided, they appear in two formats:

| Document | Department | Format | Parsing Complexity |
|---|---|---|---|
| Health Office Q1 2026 | Codes / Health | Clean text PDF | Low |
| Facilities & Special Projects Q1 2026 | Facilities | Clean text PDF | Low |
| Department of Public Works Q1 2026 | Public Works | Slide deck PDF | High |
| Building & Housing Development Q1 2026 | DEDBH | Slide deck PDF | High |

### 2.2 Quarterly Report Characteristics

Quarterly reports consistently contain the following section types across all departments, though the order and exact naming varies:

- **Description / Background** — what the department does, its mission
- **Quarterly Summary** — what happened this quarter, key activities
- **Metrics and Counts** — inspection numbers, tonnage, work orders, call volumes
- **Budget / Expenditure Tables** — account numbers, revised budgets, YTD spend
- **Annual Goals** — department goals for the year and progress updates
- **Special Projects** — notable projects, capital work, grants
- **Vacancy Updates** — open and filled positions
- **Community Engagement** — outreach activities, communications

These sections are consistent enough that section-aware chunking rules can be written specifically for quarterly reports without needing a generic document type discovery system yet.

### 2.3 Quarterly Update Cycle

New quarterly reports are produced each quarter (Q1–Q4) per department. The system must support incremental ingestion of new quarterly documents without reprocessing the existing corpus.

---

## 🔮 Future Document Types (Not In Current Build)

The following document types exist in the city's document corpus and will be ingested in future phases. They are fully designed in this specification but are **not being implemented now**. The current architecture is built to accommodate them without structural changes.

### Why They Are Excluded From the Initial Build

Each document type requires its own chunking strategy, content type rules, SQL schema extensions, and graph schema extensions. Building all of them simultaneously would make the initial build significantly more complex and harder to test. Starting with quarterly reports gives a clean, well-understood corpus to validate the full pipeline before extending it.

### Future Document Type: Resolutions

Resolutions are formal actions by City Council authorizing specific activities — contracts, expenditures, policy positions.

**Structure:**
```
RESOLUTION NO. YYYY-R-NNN
Title / subject line
WHEREAS clauses (reasoning)
RESOLVED clause (the actual authorization)
Adoption date
Vote record (council members, yes/no/abstain)
```

**What they add to the knowledge base:**
- What City Council has authorized
- Dollar amounts tied to specific authorizations
- Vendor and contractor relationships
- Voting records by council member
- Links between resolutions and projects/departments

**Chunking approach:** keep WHEREAS clauses and RESOLVED clause together or heavily cross-referenced. Never split the reasoning from the conclusion.

**New content types needed:** `legal_authorization`, `whereas_clause`, `vote_record`, `vendor_contract`

**New SQL tables needed:**
```sql
resolutions (resolution_number, title, adopted_date, amount, vendor, department, status)
votes (resolution_id, council_member, vote, vote_date)
```

**New graph nodes/relationships needed:**
```
Resolution node
Vendor node
CouncilMember node

(Resolution) -[AUTHORIZES]->        (Project)
(Resolution) -[AWARDS_CONTRACT_TO]-> (Vendor)
(CouncilMember) -[VOTED_YES/NO]->   (Resolution)
(Department) -[REQUESTED]->         (Resolution)
```

### Future Document Type: Ordinances

Ordinances are laws passed by City Council. Similar structure to resolutions but carry legal weight as municipal law.

**Additions needed:** ordinances table in SQL, Ordinance node in graph, `legal_language` content type.

### Future Document Type: Meeting Minutes

Official records of City Council and committee meetings — who attended, what was discussed, what actions were taken.

**Additions needed:** `meeting_action_item` and `discussion_summary` content types, Meeting and Attendee nodes in graph, minutes table in SQL for attendance and actions.

### Future Document Type: Contracts and Agreements

Formal contracts between the city and vendors, developers, or other entities.

**Additions needed:** contracts table in SQL, Contract and Vendor nodes in graph, `contract_term` content type.

### Future Document Type: Budget Documents

Annual citywide budget documents, distinct from the quarterly budget tables in department reports.

**Additions needed:** annual_budget table in SQL with fiscal year granularity, cross-department aggregation queries.

### Future Document Type: Grant Applications

Documents submitted to obtain grant funding, and award letters confirming grant receipt.

**Additions needed:** grant_applications table, GrantProgram node in graph, `grant_requirement` content type.

### Document Type Registry (Future)

When future document types are added, a document type registry table will store the chunking strategy, content type rules, and extraction templates for each type. When an unrecognized document is uploaded, the system will check the registry first, and if no match is found, trigger a discovery workflow where an LLM proposes a strategy for human review before ingestion.

```sql
CREATE TABLE document_type_registry (
    type_id             SERIAL PRIMARY KEY,
    type_name           VARCHAR(100),     -- 'resolution', 'ordinance', etc.
    display_name        VARCHAR(100),
    chunking_strategy   JSONB,            -- rules for this document type
    content_type_rules  JSONB,            -- classification rules
    extraction_templates JSONB,           -- prompts for SQL/graph extraction
    sql_tables          VARCHAR[],        -- which tables receive data
    graph_node_types    VARCHAR[],        -- which node types are created
    date_added          TIMESTAMP,
    added_by            VARCHAR(100),
    active              BOOLEAN DEFAULT TRUE
);
```

---

## 3. System Architecture

### 3.1 High-Level Overview

```
DOCUMENTS
    ↓
INGESTION LAYER
    ↓              ↓               ↓
Vector Store    SQL Database    Graph Database
    ↓              ↓               ↓
           ORCHESTRATION LAYER
                   ↓
            QUERY INTERFACE
                   ↓
           EVALUATION LAYER
                   ↓
         FEEDBACK + IMPROVEMENT
```

### 3.2 The Three Storage Systems

The system uses three storage backends, each optimized for a different type of information and query pattern.

#### Vector Store (Qdrant)

Stores all document content as semantic embeddings for meaning-based search.

- **What goes here:** every chunk from every document, always
- **Good for:** narrative questions, conceptual queries, vague or open-ended questions
- **Example queries it handles well:**
  - "What are the Engineering Department's goals for 2026?"
  - "What happened at the Ice and Fire Festival?"
  - "What sustainability initiatives is the city working on?"
- **Search method:** hybrid — semantic vector search combined with BM25 keyword search, results fused via Reciprocal Rank Fusion

#### SQL Database (PostgreSQL)

Stores structured, numeric data extracted from tables and metrics sections.

- **What goes here:** budget tables, expenditure figures, inspection counts, tonnage metrics, grant amounts, any structured numeric data
- **Good for:** precise numeric questions, comparisons, aggregations
- **Example queries it handles well:**
  - "How much has been spent on disposal year to date?"
  - "Which department has the highest contracted services expenditure?"
  - "How many potholes were repaired in Q1 2026?"
- **Search method:** LLM-generated SQL queries against structured schema

#### Graph Database (Neo4j)

Stores entities and relationships extracted from organizational and project data.

- **What goes here:** people, departments, roles, project ownership, reporting relationships, grant ownership
- **Good for:** organizational questions, accountability queries, relationship traversal
- **Example queries it handles well:**
  - "Who manages the Sanitation Department?"
  - "What projects is Joel Seiders responsible for?"
  - "Which departments reported open vacancies in Q1 2026?"
- **Search method:** LLM-generated Cypher queries

---

## 4. Ingestion Pipeline

### 4.1 Pipeline Overview

Every document passes through the same five-step pipeline regardless of type. The pipeline runs once on initial ingestion and again each quarter for new documents.

```
Step 1: Document Type Detection
Step 2: Parsing (extract raw text)
Step 3: Chunking (break into pieces)
Step 4: Metadata Tagging
Step 5: Classification + Storage Routing
```

### 4.2 Step 1 — Document Type Detection

Before parsing, the system detects what kind of document it is dealing with. This determines which parser to use.

```
Detection logic:

File extension check → .pdf, .docx, .pptx, .html

For PDFs specifically:
- Does it have a text layer? (pdfplumber check)
- What is the text-to-page ratio?
- Are there embedded images?
- Does the extracted text look garbled or too sparse?

Output: document_type classification
  → clean_text_pdf
  → complex_pdf (slide deck, dark backgrounds)
  → word_doc
  → other
```

### 4.3 Step 2 — Parsing

Different parsers are used based on document type. The goal of this step is to extract all text content from the document into a clean, usable form.

#### Parser A: Unstructured.io (for clean documents)

Used for clean text PDFs and Word documents. Makes zero LLM calls.

- Extracts text content with layout awareness
- Detects element types: Title, NarrativeText, Table, ListItem, Header
- Preserves reading order
- Handles basic table extraction
- Returns structured element objects with type labels already attached

**Documents handled:** Health Office report, Facilities report, clean text PDFs generally

#### Parser B: Vision LLM (for complex documents)

Used for slide deck PDFs, documents with dark backgrounds, colored table cells, and embedded org charts. Each page is rendered as an image and sent to a vision-capable LLM (GPT-4o or Claude) with an extraction prompt.

LLM call count: 1 per page for affected documents only.

Extraction prompt instructs the model to:
- Extract all visible text regardless of background color
- Preserve table structure as structured data
- Identify people and their roles from org charts
- Flag any content it cannot confidently extract

**Documents handled:** Public Works slide deck, DEDBH report, any future slide deck documents

#### Parser C: Fallback Logic

Unstructured.io attempts parsing first on all documents. A quality check evaluates the output:

- Is the extracted text suspiciously short for the document size?
- Is the garbled character ratio above 10%?
- Were tables detected but no structured data returned?

If the quality check fails, the document or specific pages are re-parsed using the Vision LLM parser.

### 4.4 Step 3 — Chunking

After parsing, the raw extracted text is broken into chunks. Chunking follows structural boundaries in the document, not fixed token counts.

**Chunking rules:**

- Chunk at section header boundaries (new section = new chunk)
- For slide deck documents, each slide is a natural chunk boundary
- Tables are kept as single chunks even if large — never split a table across chunks
- Lists are kept together as a single chunk
- Minimum chunk size: 100 characters
- Maximum chunk size: 1,500 characters (split at paragraph boundary if exceeded)
- Overlap: 100 character overlap between adjacent narrative chunks to preserve context across boundaries

**Example — Health Office report chunked at section boundaries:**

```
Chunk 1: Description section (narrative)
Chunk 2: Quarterly Summary - narrative paragraph
Chunk 3: Quarterly Summary - inspection counts list
Chunk 4: Annual Goal Updates
Chunk 5: Grant Update - narrative
Chunk 6: Grant Update - grant amounts breakdown
Chunk 7: Special Projects
Chunk 8: Department Vacancy Updates
Chunk 9: Community Engagement
```

### 4.5 Step 4 — Metadata Tagging

Every chunk receives a metadata object before storage. Metadata enables filtered retrieval — queries can be scoped to a specific department, time period, or section without searching the entire corpus.

**Metadata schema:**

```json
{
  "chunk_id": "uuid",
  "source_file": "Health_Office_Q1_2026.pdf",
  "department": "Health Office",
  "document_type": "quarterly_report",
  "quarter": "Q1",
  "year": 2026,
  "section": "Grant Update",
  "content_type": "narrative | table | metrics | org_data | project",
  "page_number": 3,
  "parser_used": "unstructured | vision_llm",
  "ingestion_timestamp": "2026-06-01T00:00:00Z",
  "chunk_index": 5,
  "total_chunks_in_doc": 9
}
```

### 4.6 Step 5 — Classification and Storage Routing

Each chunk is classified by content type, which determines which storage systems receive it. The vector store always receives every chunk. SQL and Graph databases receive chunks only when relevant data can be extracted.

#### Classification Method

Classification uses a hybrid approach:

**Rule-based first (no LLM call):**
- Unstructured.io element type is `Table` → classify as `table`
- Unstructured.io element type is `Title` → classify as `header`
- Line numeric ratio > 60% → classify as `metrics`
- Contains org keywords (manages, director, reports to, led by) → classify as `org_data`
- Default → classify as `narrative`

**LLM fallback (1 LLM call) for ambiguous cases:**
- Chunk has mixed signals (narrative + numbers)
- Chunk type cannot be determined from rules alone
- Approximately 30% of chunks require this fallback

#### Storage Routing by Content Type

| Content Type | Vector Store | SQL Database | Graph Database |
|---|---|---|---|
| narrative | ✓ | — | — |
| table (budget) | ✓ | ✓ | ✓ |
| metrics | ✓ | ✓ | — |
| org_data | ✓ | — | ✓ |
| project | ✓ | — | ✓ |
| header | ✓ | — | — |

#### Extraction for SQL and Graph

When a chunk is routed to SQL or Graph, a structured extraction step runs using an LLM (1 call per chunk requiring extraction):

**SQL extraction prompt** instructs the LLM to return JSON rows:
```json
{
  "rows": [
    {
      "account_number": "25660000-422091",
      "line_item": "Disposal",
      "revised_budget": 8350000.00,
      "ytd_expended": 650198.57,
      "department": "Sanitation",
      "quarter": "Q1",
      "year": 2026
    }
  ]
}
```

**Graph extraction prompt** instructs the LLM to return JSON entities and relationships:
```json
{
  "people": [
    {"name": "Dave West", "title": "Director of Public Works"}
  ],
  "departments": [
    {"name": "Department of Public Works"}
  ],
  "relationships": [
    {
      "from": "Dave West",
      "relationship": "DIRECTS",
      "to": "Department of Public Works"
    }
  ]
}
```

Extraction calls are batched — up to 8 similar chunks sent in a single LLM call to reduce cost.

### 4.7 LLM Call Estimates Per Document

| Document Type | Parsing | Classification | Extraction | Total |
|---|---|---|---|---|
| Clean text PDF (~30 chunks) | 0 | ~9 | ~12 | ~21 |
| Complex slide deck (~60 chunks, 30 pages) | ~30 | ~18 | ~8 (batched) | ~56 |

For an initial corpus of ~50 documents (mixed types):  
**Estimated total ingestion LLM calls: ~1,500–2,500**  
**Estimated one-time ingestion cost: $15–40**

---

## 5. SQL Database Schema

### 5.1 Core Tables

```sql
-- All budget and expenditure data
CREATE TABLE expenditures (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    sub_department  VARCHAR(100),
    account_number  VARCHAR(50),
    line_item       VARCHAR(200),
    revised_budget  DECIMAL(15,2),
    ytd_expended    DECIMAL(15,2),
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    source_file     VARCHAR(255),
    ingested_at     TIMESTAMP
);

-- Performance metrics and counts
CREATE TABLE metrics (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    metric_name     VARCHAR(200),
    metric_value    DECIMAL(15,2),
    metric_unit     VARCHAR(50),
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    source_file     VARCHAR(255),
    ingested_at     TIMESTAMP
);

-- Grant information
CREATE TABLE grants (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    grant_name      VARCHAR(255),
    grant_number    VARCHAR(100),
    amount          DECIMAL(15,2),
    start_date      DATE,
    end_date        DATE,
    status          VARCHAR(50),
    source_chunk_id UUID,
    source_file     VARCHAR(255),
    ingested_at     TIMESTAMP
);

-- Vacancy tracking
CREATE TABLE vacancies (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    position_title  VARCHAR(200),
    status          VARCHAR(50),
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    ingested_at     TIMESTAMP
);
```

---

## 6. Graph Database Schema

### 6.1 Node Types

```
Person
  properties: name, title, department

Department  
  properties: name, parent_department

Project
  properties: name, status, description, location

Grant
  properties: name, grant_number, amount, status

Document
  properties: filename, quarter, year, department
```

### 6.2 Relationship Types

```
(Person)     -[DIRECTS]->        (Department)
(Person)     -[MANAGES]->        (Department)
(Person)     -[MANAGES]->        (Project)
(Person)     -[REPORTS_TO]->     (Person)
(Department) -[HAS_PROJECT]->    (Project)
(Department) -[REPORTED_IN]->    (Document)
(Department) -[MANAGES_GRANT]->  (Grant)
(Project)    -[MENTIONED_IN]->   (Document)
```

---

## 7. Query Pipeline

### 7.1 Query Flow Overview

```
User question
      ↓
Step 1: Query Classification (1 LLM call)
      ↓
Step 2: Retrieval (0 LLM calls - pure DB/search ops)
      ↓
Step 3: Answer Synthesis (1 LLM call)
      ↓
Answer with citations
      ↓
Step 4: Automatic Evaluation (1 LLM call - async)
```

**Total synchronous LLM calls per query: 2**  
**Total including async evaluation: 3**

### 7.2 Step 1 — Query Classification

An LLM reads the question and produces a retrieval plan as structured JSON:

```json
{
  "sources": ["sql", "vector"],
  "execution": "parallel",
  "sequential_order": null,
  "sql_query": "SELECT department, SUM(ytd_expended) FROM expenditures WHERE quarter='Q1' AND year=2026 GROUP BY department ORDER BY SUM(ytd_expended) DESC",
  "vector_query": "department expenditure spending Q1 2026",
  "graph_query": null,
  "reasoning": "Question asks for numeric spending data (SQL) with possible narrative context (vector). No relationship traversal needed."
}
```

The classifier also extracts any metadata filters from the question (specific department, specific quarter, specific year) to pass to the vector store for pre-filtering.

### 7.3 Step 2 — Retrieval

Retrieval is pure database and search operations. No LLM calls in this step.

**Vector retrieval:** hybrid search (semantic + BM25) with metadata pre-filtering, returns top 5 chunks with relevance scores.

**SQL retrieval:** executes the generated SQL query against PostgreSQL, returns structured rows.

**Graph retrieval:** executes the generated Cypher query against Neo4j, returns entity and relationship data.

For parallel execution, all applicable stores are queried simultaneously. For sequential execution, the result from the first store is passed as additional context when querying the second store.

### 7.4 Step 3 — Answer Synthesis

All retrieved results are passed to an LLM with the original question. The synthesis prompt instructs the model to:

- Answer the question using only the retrieved information
- Cite the source document and section for each piece of information
- Flag any conflicts between sources explicitly
- State clearly if the information needed to answer the question was not found
- Never guess or fill in numbers from memory

### 7.5 Execution Modes

| Mode | When Used | Stores Queried | LLM Calls |
|---|---|---|---|
| Vector only | Narrative/conceptual questions | Vector | 2 |
| SQL only | Pure numeric questions | SQL | 2 |
| Graph only | Pure relationship questions | Graph | 2 |
| Parallel | Independent sub-questions | Multiple simultaneously | 2 |
| Sequential | Answer from store A needed to query store B | Multiple in order | 3 |
| Fallback | Initial retrieval returned weak results | Retry with different query | 3 |

---

## 8. Improvement System

### 8.1 Query Logging

Every query is logged in full detail to a PostgreSQL table:

```sql
CREATE TABLE query_logs (
    query_id        UUID PRIMARY KEY,
    question        TEXT,
    timestamp       TIMESTAMP,
    classification  JSONB,
    sql_query       TEXT,
    chunks_retrieved JSONB,
    stores_queried  VARCHAR[],
    sql_results     JSONB,
    vector_results  JSONB,
    graph_results   JSONB,
    final_answer    TEXT,
    citations       JSONB,
    total_time_ms   INTEGER,
    retrieval_score DECIMAL(3,2),
    accuracy_score  DECIMAL(3,2),
    completeness_score DECIMAL(3,2),
    user_feedback   VARCHAR(20),
    user_notes      TEXT,
    correct_answer  TEXT
);
```

### 8.2 Automatic Evaluation

After every query, an async LLM call evaluates the answer quality on three dimensions (1–5 scale):

- **Retrieval score:** did the system retrieve chunks that actually contain the answer?
- **Accuracy score:** does the answer correctly reflect what was in the retrieved chunks?
- **Completeness score:** is anything important missing from the answer?

Additional flags:
- `retrieval_failure`: true if no relevant chunks were found
- `hallucination_detected`: true if the answer contains information not in retrieved chunks

### 8.3 User Feedback Interface

A simple thumbs up/down control on every answer response. On thumbs down, user is prompted:

```
What was wrong with this answer?
○ Wrong number or figure
○ Wrong person or department
○ Missing important information
○ Answer was off topic
○ Other: ____________
```

### 8.4 Chunk Performance Tracking

```sql
CREATE TABLE chunk_performance (
    chunk_id            UUID PRIMARY KEY,
    times_retrieved     INTEGER DEFAULT 0,
    times_good_answer   INTEGER DEFAULT 0,
    times_bad_answer    INTEGER DEFAULT 0,
    quality_score       DECIMAL(3,2),
    last_retrieved      TIMESTAMP,
    flagged_for_review  BOOLEAN DEFAULT FALSE
);
```

Chunks with quality scores below 2.5 after 5+ retrievals are automatically flagged for manual review.

### 8.5 Quarterly Improvement Cycle

At the start of each new quarter, before ingesting new documents, a review process runs:

**Step 1: Performance Review**
- Pull all queries from previous quarter with score below 3.0
- Identify failure patterns (which question types, which departments, which stores)
- Generate a failure analysis report using LLM

**Step 2: Fix Identified Issues**
- Re-chunk documents that produced low-quality chunks
- Fix incorrect SQL or Graph extractions
- Update classification rules for newly discovered edge cases
- Update synthesis prompts if hallucination was detected

**Step 3: Ingest New Documents**
- Apply improved pipeline to new quarterly documents
- New documents benefit from all fixes made in the review step

**Step 4: Evaluation Suite Run**
- Run all 50+ known questions with known answers
- Compare scores to previous quarter
- Confirm improvement before marking ingestion complete

**Step 5: Coverage Gap Report**
- Identify questions that returned no useful results
- Report which departments or topics lack coverage
- Flag which document types should be prioritized for next ingestion

### 8.6 Evaluation Suite

A maintained set of questions with known correct answers, categorized by storage system and document type. Minimum 50 questions at launch, growing each quarter.

| Category | Example Question | Expected Answer | Store |
|---|---|---|---|
| Budget | How much was spent on disposal in Q1 2026? | $650,198.57 | SQL |
| Org | Who is the Director of Public Works? | Dave West | Graph |
| Narrative | What is the Health Office responsible for? | Food safety compliance... | Vector |
| Cross-store | What department spent most on contracted services and who leads it? | Highway / John Watson | SQL + Graph |
| Grant | What grant did the Health Office receive in 2026? | NEHA-FDA, $14,000 | Vector + SQL |

The evaluation suite runs automatically on any of these triggers:
- New documents ingested
- Chunking strategy changed
- Any prompt updated
- New document type added

---

## 9. Technology Stack

### 9.1 Core Components

| Component | Technology | Purpose |
|---|---|---|
| Document parsing (clean) | Unstructured.io (open source) | Text PDF and Word doc parsing |
| Document parsing (complex) | GPT-4o Vision or Claude | Slide deck and complex layout parsing |
| Embeddings | OpenAI text-embedding-3-large | Convert text to vectors |
| Vector store | Qdrant (self-hosted) | Semantic and hybrid search |
| SQL database | PostgreSQL | Structured numeric and budget data |
| Graph database | Neo4j | Entity and relationship queries |
| Orchestration | Plain Python (async) | Query routing and pipeline control |
| RAG framework | LlamaIndex | Document loading, chunking, vector retrieval |
| LLM (queries) | Claude Sonnet or GPT-4o | Classification, synthesis, evaluation |
| Observability | LangSmith | Query tracing, debugging, monitoring |
| API layer | FastAPI | REST API for query interface |
| Background jobs | Celery + Redis | Async evaluation, ingestion jobs |

### 9.2 Why These Choices

**Qdrant over Pinecone:** self-hosted, no per-vector cost, supports hybrid search natively, open source.

**LlamaIndex over LangChain:** purpose-built for document ingestion and retrieval, cleaner query engine abstraction, better multi-index routing.

**Plain Python orchestration over LangGraph:** the routing logic (3 stores, 5 execution modes) is simple enough that a framework adds complexity without value. Every step is transparent and debuggable.

**PostgreSQL for SQL layer:** already widely used in government infrastructure, reliable, supports JSONB for flexible metadata, pgvector available if needed.

---

## 10. Build Order and Phases

### Current Build (Claude Code) — Quarterly Reports Only

The following phases are what is being built now. All phases are scoped exclusively to quarterly report documents. No other document types are ingested, no resolutions schema is created, no vote record extraction is implemented.

### Phase 1 — Core RAG (Weeks 1–2)

Goal: basic working system for clean text quarterly reports.

- Set up Unstructured.io parser for text PDFs
- Implement quarterly-report-aware chunking with section boundary detection
- Tag all chunks with quarterly report metadata schema (department, quarter, year, section)
- Set up Qdrant with hybrid search (vector + BM25)
- Implement simple query → vector search → LLM synthesis pipeline
- Ingest Health Office and Facilities quarterly reports
- Test with 10 narrative questions drawn from those documents
- Set up LangSmith for tracing

**Success criteria:** 7/10 narrative questions answered correctly with correct citations.

### Phase 2 — SQL Layer (Weeks 3–4)

Goal: numeric and budget questions from quarterly reports work.

- Design and create PostgreSQL schema for quarterly report data (expenditures, metrics, grants, vacancies tables)
- Build table extraction pipeline (LLM extraction to JSON rows)
- Implement query classifier (vector vs SQL routing)
- Ingest all budget tables from quarterly report documents into SQL
- Test with 10 numeric questions

**Success criteria:** 8/10 numeric questions return exact correct figures.

### Phase 3 — Complex Document Parsing (Weeks 4–5)

Goal: slide deck quarterly reports (Public Works, DEDBH) are ingested correctly.

- Implement Vision LLM parser for complex PDFs
- Implement quality check fallback logic (Unstructured first, Vision LLM on failure)
- Re-ingest Public Works and DEDBH quarterly reports
- Verify white-on-black text extracted correctly
- Verify colored budget table cells extracted correctly
- Verify org chart data captured

**Success criteria:** Public Works org chart and budget tables fully ingested and queryable.

### Phase 4 — Graph Layer (Weeks 6–7)

Goal: organizational and relationship questions from quarterly reports work.

- Set up Neo4j
- Implement quarterly-report graph schema (Person, Department, Project, Grant nodes)
- Build org entity extraction pipeline
- Ingest all people, departments, reporting relationships, and project ownership from quarterly reports
- Add graph routing to query classifier
- Test with 10 relationship questions

**Success criteria:** "Who manages X department?" answered correctly for all departments in quarterly report corpus.

### Phase 5 — Improvement System (Weeks 8–9)

Goal: system can measure and improve its own quality.

- Implement query logging
- Implement automatic LLM evaluation (async, runs after every query)
- Build user feedback UI (thumbs up/down with failure category)
- Implement chunk performance tracking
- Build evaluation suite (50 known Q&A pairs from quarterly reports)
- Run first full evaluation suite, establish baseline scores

**Success criteria:** all query types logged, evaluation suite running, baseline scores recorded.

### Phase 6 — Quarterly Ingestion Workflow (Week 10)

Goal: adding new quarterly reports each quarter is a defined, repeatable process.

- Automate ingestion pipeline trigger on new document upload
- Build quarterly review report generator (pulls low-scoring queries, identifies patterns)
- Build coverage gap detector (identifies departments or quarters missing from corpus)
- Document the quarterly process end to end
- Test with a simulated Q2 batch of quarterly reports

**Success criteria:** new quarterly reports ingested correctly and evaluation suite score does not drop.

---

### 🔮 Future Phases (Post-Quarterly-Report Build)

These phases are designed but not being implemented yet. They extend the system to handle additional document types without restructuring anything built in Phases 1–6.

**Phase 7 — Resolutions**
Add resolutions schema to SQL and graph. Build resolution-aware chunking. Ingest resolution documents. Add resolution-specific content types (legal_authorization, whereas_clause, vote_record).

**Phase 8 — Meeting Minutes**
Add minutes schema. Build action-item extraction. Link meeting decisions to departments and projects in graph.

**Phase 9 — Ordinances and Contracts**
Extend resolution schema for ordinances. Add contract and vendor nodes to graph. Build contract term extraction.

**Phase 10 — Document Type Discovery**
Build document type registry. Build LLM-assisted strategy proposal for unknown document types. Build human review workflow for new type approval.

---

## 11. Cost Estimates

### 11.1 One-Time Ingestion Cost

| Item | Estimate |
|---|---|
| Vision LLM parsing (complex docs, ~150 pages) | $2–5 |
| Classification LLM calls (~750 calls) | $1–2 |
| Extraction LLM calls (~500 calls, batched) | $2–4 |
| Embeddings (~3,000 chunks) | <$0.50 |
| **Total initial ingestion** | **~$15–40** |

### 11.2 Ongoing Query Cost

| Volume | Cost per Query | Monthly Cost |
|---|---|---|
| 50 queries/day | ~$0.08 | ~$120 |
| 100 queries/day | ~$0.08 | ~$240 |
| 200 queries/day | ~$0.08 | ~$480 |

### 11.3 Quarterly Re-Ingestion Cost

Each new batch of ~10 documents (mix of clean and complex):  
**Estimated: $5–15 per quarterly update**

### 11.4 Infrastructure Cost (Self-Hosted)

| Service | Monthly Cost |
|---|---|
| Qdrant (self-hosted, small VM) | ~$20–50 |
| PostgreSQL (managed, small instance) | ~$20–50 |
| Neo4j (self-hosted or AuraDB free tier) | $0–50 |
| Redis (for Celery) | ~$10–20 |
| **Total infrastructure** | **~$50–170/month** |

---

## 12. Open Questions

The following decisions are deferred pending further discussion:

1. **Authentication:** who has access to the query interface? Is there role-based access (some employees see all departments, others only their own)?

2. **Document upload workflow:** who is responsible for uploading new quarterly documents? Is there an approval step before ingestion?

3. **Answer confidence display:** should low-confidence answers be flagged visually to the user?

4. **Hallucination policy:** if the system cannot find information to answer a question, should it say so explicitly or attempt a best-guess answer? Recommendation is explicit "not found" rather than guessing, especially for a government system.

5. **Graph database hosting:** Neo4j self-hosted vs AuraDB (managed cloud). AuraDB has a free tier that may be sufficient for quarterly reports corpus size. Decision can be deferred until Phase 4.

6. **LLM provider:** Claude vs GPT-4o for query-time LLM calls. Both work. Decision may depend on existing vendor relationships or data residency requirements for government use.

7. **Data retention:** how long are query logs retained? Are there government record-keeping requirements that apply to query history?

8. **Future document type prioritization:** after quarterly reports are working, which document type gets added next — resolutions, meeting minutes, or something else? This affects which SQL and graph schema extensions to design first.

9. **Claude Code handoff:** the spec as written gives Claude Code enough context to build Phases 1–6 for quarterly reports. The 🔮 future sections are included for awareness but should be explicitly excluded from the current implementation scope. When handing this spec to Claude Code, instruct it to build only what is described in Phases 1–6 and to design the database schemas with future extensibility in mind (no hardcoded assumptions that quarterly reports are the only document type) but not to implement any future phase logic.

---

## 13. Glossary

| Term | Definition |
|---|---|
| Chunk | A small piece of text extracted from a document, stored as a unit in the vector store |
| Embedding | A numerical representation of text that captures its meaning, used for semantic search |
| Vector store | A database optimized for storing and searching embeddings |
| Hybrid search | Combining semantic vector search with keyword (BM25) search |
| RAG | Retrieval-Augmented Generation — the pattern of retrieving relevant documents before generating an answer |
| Ingestion | The process of reading documents, extracting content, and loading it into storage systems |
| Orchestration | The logic that routes a question to the right storage systems and combines the results |
| Evaluation suite | A fixed set of questions with known correct answers used to measure system quality |
| BM25 | A keyword-based ranking algorithm used in traditional search engines |
| Cypher | The query language used by Neo4j graph database |
| Resolution | A formal City Council action authorizing a specific activity, expenditure, or contract |
| Ordinance | A law passed by City Council, similar structure to a resolution but with legal permanence |
| WHEREAS clause | The reasoning section of a resolution or ordinance, explaining why an action is being taken |
| RESOLVED clause | The action section of a resolution — the actual thing being authorized |
| Document type registry | A database table storing chunking strategies and extraction rules for each known document type |
| Content type | A classification applied to each chunk indicating what kind of information it contains (narrative, table, metrics, org_data, etc.) |
| 🔮 | Used throughout this document to mark sections that are designed but not part of the current Claude Code implementation |
