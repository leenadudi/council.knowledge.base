-- Harrisburg Knowledge Base — PostgreSQL Schema
-- Run this once to initialize the database.

-- Budget and expenditure data extracted from quarterly reports
CREATE TABLE IF NOT EXISTS expenditures (
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
    ingested_at     TIMESTAMP DEFAULT NOW()
);

-- Performance metrics and counts from quarterly reports
CREATE TABLE IF NOT EXISTS metrics (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    metric_name     VARCHAR(200),
    metric_value    DECIMAL(15,2),
    metric_unit     VARCHAR(50),
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    source_file     VARCHAR(255),
    ingested_at     TIMESTAMP DEFAULT NOW()
);

-- Grant information extracted from quarterly reports
CREATE TABLE IF NOT EXISTS grants (
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
    ingested_at     TIMESTAMP DEFAULT NOW()
);

-- Vacancy tracking from quarterly reports
CREATE TABLE IF NOT EXISTS vacancies (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    position_title  VARCHAR(200),
    status          VARCHAR(50),
    open_count      INTEGER,            -- number of open positions of this title (from "Patrol Officer- (25)")
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    source_file     VARCHAR(255),       -- delete-by-file on re-ingest, like every other structured table
    ingested_at     TIMESTAMP DEFAULT NOW()
);

-- Special-project / initiative tracking from quarterly reports
CREATE TABLE IF NOT EXISTS projects (
    id              SERIAL PRIMARY KEY,
    department      VARCHAR(100),
    project_name    VARCHAR(300),
    description     TEXT,
    status          VARCHAR(50),
    funding_source  VARCHAR(200),
    quarter         VARCHAR(5),
    year            INTEGER,
    source_chunk_id UUID,
    source_file     VARCHAR(255),
    ingested_at     TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_dept ON projects(department);

-- Document ingestion tracking
CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    source_file     VARCHAR(255) UNIQUE,
    department      VARCHAR(100),
    document_type   VARCHAR(50),
    quarter         VARCHAR(5),
    year            INTEGER,
    parser_used     VARCHAR(50),
    total_chunks    INTEGER,
    ingested_at     TIMESTAMP DEFAULT NOW(),
    reingested_at   TIMESTAMP
);

-- Full query log for every query answered by the system
CREATE TABLE IF NOT EXISTS query_logs (
    query_id            UUID PRIMARY KEY,
    question            TEXT,
    timestamp           TIMESTAMP DEFAULT NOW(),
    classification      JSONB,
    sql_query           TEXT,
    chunks_retrieved    JSONB,
    stores_queried      VARCHAR[],
    sql_results         JSONB,
    vector_results      JSONB,
    graph_results       JSONB,
    final_answer        TEXT,
    citations           JSONB,
    total_time_ms       INTEGER,
    retrieval_score     DECIMAL(3,2),
    accuracy_score      DECIMAL(3,2),
    completeness_score  DECIMAL(3,2),
    user_feedback       VARCHAR(20),
    user_notes          TEXT,
    correct_answer      TEXT,
    clarity_assessment  JSONB
);

-- Chunk performance tracking for quality improvement
CREATE TABLE IF NOT EXISTS chunk_performance (
    chunk_id            UUID PRIMARY KEY,
    times_retrieved     INTEGER DEFAULT 0,
    times_good_answer   INTEGER DEFAULT 0,
    times_bad_answer    INTEGER DEFAULT 0,
    quality_score       DECIMAL(3,2),
    last_retrieved      TIMESTAMP,
    flagged_for_review  BOOLEAN DEFAULT FALSE
);

-- Evaluation suite: known Q&A pairs used for automated scoring
CREATE TABLE IF NOT EXISTS evaluation_suite (
    id              SERIAL PRIMARY KEY,
    question        TEXT NOT NULL,
    expected_answer TEXT NOT NULL,
    store_type      VARCHAR(50),   -- 'sql', 'vector', 'graph', 'cross'
    department      VARCHAR(100),
    quarter         VARCHAR(5),
    year            INTEGER,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Evaluation run results (one row per question per run)
CREATE TABLE IF NOT EXISTS evaluation_results (
    id              SERIAL PRIMARY KEY,
    run_id          UUID NOT NULL,
    run_date        TIMESTAMP DEFAULT NOW(),
    question_id     INTEGER REFERENCES evaluation_suite(id),
    question        TEXT,
    expected_answer TEXT,
    actual_answer   TEXT,
    retrieval_score DECIMAL(3,2),
    accuracy_score  DECIMAL(3,2),
    completeness_score DECIMAL(3,2),
    passed          BOOLEAN,
    notes           TEXT
);

-- Document type registry (extensible for future document types)
CREATE TABLE IF NOT EXISTS document_type_registry (
    type_id             SERIAL PRIMARY KEY,
    type_name           VARCHAR(100),
    display_name        VARCHAR(100),
    description         TEXT,
    chunking_strategy   JSONB,
    content_type_rules  JSONB,
    extraction_templates JSONB,
    sql_tables          VARCHAR[],
    graph_node_types    VARCHAR[],
    date_added          TIMESTAMP DEFAULT NOW(),
    added_by            VARCHAR(100),
    active              BOOLEAN DEFAULT TRUE
);

-- Seed the quarterly_report document type
INSERT INTO document_type_registry (
    type_name, display_name, chunking_strategy, content_type_rules,
    sql_tables, graph_node_types, added_by
) VALUES (
    'quarterly_report',
    'Quarterly Report',
    '{"method": "section_boundary", "max_chunk_size": 1500, "min_chunk_size": 100, "overlap": 100, "slide_per_chunk": true}',
    '{"table": "element_type=Table", "metrics": "numeric_ratio>0.6", "org_data": "org_keywords_present", "narrative": "default"}',
    ARRAY['expenditures', 'metrics', 'grants', 'vacancies'],
    ARRAY['Person', 'Department', 'Project', 'Grant'],
    'system'
) ON CONFLICT DO NOTHING;

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_expenditures_dept_quarter ON expenditures(department, quarter, year);
CREATE INDEX IF NOT EXISTS idx_metrics_dept_quarter ON metrics(department, quarter, year);
CREATE INDEX IF NOT EXISTS idx_grants_dept ON grants(department);
CREATE INDEX IF NOT EXISTS idx_vacancies_dept_quarter ON vacancies(department, quarter, year);
CREATE INDEX IF NOT EXISTS idx_query_logs_timestamp ON query_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_chunk_perf_quality ON chunk_performance(quality_score) WHERE quality_score < 2.5;
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON evaluation_results(run_id);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                  UUID PRIMARY KEY,
    timestamp           TIMESTAMP DEFAULT NOW(),
    call_site           VARCHAR(64),
    model               VARCHAR(64),
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    est_cost_usd        DECIMAL(10,6),
    latency_ms          INTEGER,
    query_id            UUID,
    batch_id            VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_timestamp ON llm_usage (timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_usage_call_site ON llm_usage (call_site);

-- City Council resolutions: formal authorization actions
CREATE TABLE IF NOT EXISTS resolutions (
    id                 SERIAL PRIMARY KEY,
    resolution_number  VARCHAR(50),
    title              TEXT,
    amount             DECIMAL(15,2),
    vendor             VARCHAR(255),
    department         VARCHAR(100),
    adopted_date       DATE,
    status             VARCHAR(50),
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

-- Individual council member votes on resolutions
CREATE TABLE IF NOT EXISTS votes (
    id                 SERIAL PRIMARY KEY,
    resolution_number  VARCHAR(50),
    council_member     VARCHAR(120),
    vote               VARCHAR(50),
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_resolutions_number ON resolutions(resolution_number);
CREATE INDEX IF NOT EXISTS idx_resolutions_vendor ON resolutions(vendor);
CREATE INDEX IF NOT EXISTS idx_votes_resolution ON votes(resolution_number);

-- Documents/rows withheld from structured tables pending human review.
CREATE TABLE IF NOT EXISTS review_flags (
    id           SERIAL PRIMARY KEY,
    source_file  VARCHAR(255) NOT NULL,
    stage        VARCHAR(20)  NOT NULL,   -- parse | classify | validate
    reason       TEXT         NOT NULL,
    detail       TEXT,
    resolved     BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_review_flags_unresolved ON review_flags(resolved);

-- Agent-proposed structured-data types/mappings for unclassified docs, awaiting human
-- review (see docs/superpowers/specs/2026-07-14-adaptive-structured-ingestion-design.md).
CREATE TABLE IF NOT EXISTS type_proposals (
    id            SERIAL PRIMARY KEY,
    source_file   VARCHAR(255) NOT NULL,
    proposed_type VARCHAR(100),
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    payload       JSONB NOT NULL,
    created_at    TIMESTAMP DEFAULT NOW(),
    reviewed_at   TIMESTAMP,
    reviewer_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_type_proposals_status ON type_proposals(status);

-- City Council legislative session minutes: one row per session
CREATE TABLE IF NOT EXISTS meetings (
    id                     SERIAL PRIMARY KEY,
    meeting_date           DATE,
    session_type           VARCHAR(60),
    president              VARCHAR(120),
    members_present        INTEGER,
    members_present_names  TEXT,
    members_absent_names   TEXT,
    call_to_order          VARCHAR(20),
    adjourned              VARCHAR(20),
    source_chunk_id        UUID,
    source_file            VARCHAR(255),
    ingested_at            TIMESTAMP DEFAULT NOW()
);

-- Actions taken during a session on resolutions / ordinances (links minutes -> resolutions/legislation)
CREATE TABLE IF NOT EXISTS meeting_actions (
    id                 SERIAL PRIMARY KEY,
    meeting_date       DATE,
    item_type          VARCHAR(30),   -- resolution | ordinance | minutes_approval | other
    item_number        VARCHAR(50),
    title              TEXT,
    action             VARCHAR(150),
    committee          VARCHAR(120),
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_date ON meeting_actions(meeting_date);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_item ON meeting_actions(item_type, item_number);

-- City Council legislation (ordinances / bills): twin of resolutions, keyed on bill number
CREATE TABLE IF NOT EXISTS legislation (
    id                 SERIAL PRIMARY KEY,
    bill_number        VARCHAR(50),
    title              TEXT,
    sponsor            VARCHAR(255),
    amount             DECIMAL(15,2),
    adopted_date       DATE,
    status             VARCHAR(60),
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

-- Department-level appropriations extracted from budget documents (where a clean table exists)
CREATE TABLE IF NOT EXISTS appropriations (
    id                 SERIAL PRIMARY KEY,
    department         VARCHAR(150),
    fiscal_year        INTEGER,
    fund               VARCHAR(100),
    category           VARCHAR(150),
    amount             DECIMAL(15,2),
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_legislation_number ON legislation(bill_number);
CREATE INDEX IF NOT EXISTS idx_appropriations_dept ON appropriations(department, fiscal_year);

-- Department goals stated in quarterly reports ("Annual Goals" sections)
CREATE TABLE IF NOT EXISTS goals (
    id                 SERIAL PRIMARY KEY,
    department         VARCHAR(150),
    year               INTEGER,
    quarter            VARCHAR(5),
    goal_title         TEXT,
    description        TEXT,
    target             TEXT,
    status             TEXT,
    user_status        TEXT,          -- clerk-set status override: not_started|in_progress|completed
    user_status_at     TIMESTAMP,     -- when the clerk last set user_status
    -- Trackability classification (AI-suggested + human-confirmed; written
    -- group-wide per logical goal). See migrate_2026_08_03_goal_trackability.sql.
    trackability            TEXT,     -- measurable|milestone|aspiration|needs_review
    trackability_confidence NUMERIC,
    trackability_rationale  TEXT,
    trackability_metric     TEXT,
    trackability_deadline   TEXT,
    trackability_source     TEXT,     -- 'ai' | 'human'
    trackability_at         TIMESTAMP,
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_goals_dept ON goals(department, year);

-- ---- 2026-07-28 Schema Relationships Migration (additive) -----------------
-- document_chunks is created by the application (VectorStore.ensure_collection),
-- not by SQL. All statements referencing document_chunks below are guarded with
-- to_regclass('document_chunks') IS NOT NULL, so this file is safe to apply before
-- app init; those constraints are added once the table exists — re-apply after first
-- app start if you bootstrapped SQL-first.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---- Entity dimensions (created empty; no seed rows) -----------------------
CREATE TABLE IF NOT EXISTS departments (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,
    canonical_key  VARCHAR(120) NOT NULL UNIQUE,
    dept_type      VARCHAR(30),
    parent_id      INTEGER REFERENCES departments(id),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS department_aliases (
    id            SERIAL PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    alias         VARCHAR(150) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_department_aliases_alias ON department_aliases(LOWER(alias));

CREATE TABLE IF NOT EXISTS council_members (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,
    canonical_key  VARCHAR(150) NOT NULL UNIQUE,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS council_member_aliases (
    id        SERIAL PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES council_members(id) ON DELETE CASCADE,
    alias     VARCHAR(150) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_member_aliases_alias ON council_member_aliases(LOWER(alias));

CREATE TABLE IF NOT EXISTS vendors (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(200) NOT NULL UNIQUE,
    canonical_key  VARCHAR(200) NOT NULL UNIQUE,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS vendor_aliases (
    id        SERIAL PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    alias     VARCHAR(200) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_vendor_aliases_alias ON vendor_aliases(LOWER(alias));

-- ---- department_id on the department-bearing tables ------------------------
-- (document_chunks handled separately below — it's app-created, not SQL-created.)
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','documents','resolutions',
    'appropriations','goals','evaluation_suite'
  ] LOOP
    EXECUTE format(
      'ALTER TABLE %I ADD COLUMN IF NOT EXISTS department_id INTEGER REFERENCES departments(id)', t);
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS idx_%s_department_id ON %I(department_id)', t, t);
  END LOOP;
END $$;

-- document_chunks.department_id (+ index), guarded on the app-created table.
-- FK added as a named constraint (not inline): vector_store.py may already have
-- created this column without a FK on a fresh deploy, in which case inline
-- REFERENCES inside ADD COLUMN IF NOT EXISTS would silently no-op.
DO $$ BEGIN
  IF to_regclass('document_chunks') IS NOT NULL THEN
    ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS department_id INTEGER;
    CREATE INDEX IF NOT EXISTS idx_document_chunks_department_id ON document_chunks(department_id);
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_chunks_department') THEN
      ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_department
        FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE SET NULL NOT VALID;
    END IF;
  END IF;
END $$;

-- ---- people + vendor FK columns on the tables that carry those strings -----
ALTER TABLE votes       ADD COLUMN IF NOT EXISTS member_id    INTEGER REFERENCES council_members(id);
ALTER TABLE meetings    ADD COLUMN IF NOT EXISTS president_id INTEGER REFERENCES council_members(id);
ALTER TABLE legislation ADD COLUMN IF NOT EXISTS sponsor_id   INTEGER REFERENCES council_members(id);
ALTER TABLE resolutions ADD COLUMN IF NOT EXISTS vendor_id    INTEGER REFERENCES vendors(id);
CREATE INDEX IF NOT EXISTS idx_votes_member_id      ON votes(member_id);
CREATE INDEX IF NOT EXISTS idx_meetings_president_id ON meetings(president_id);
CREATE INDEX IF NOT EXISTS idx_legislation_sponsor  ON legislation(sponsor_id);
CREATE INDEX IF NOT EXISTS idx_resolutions_vendor_id ON resolutions(vendor_id);

-- ---- legislative surrogate-FK columns --------------------------------------
-- FKs added as named constraints below (after the backfill), not inline here:
-- inline REFERENCES inside ADD COLUMN IF NOT EXISTS silently no-ops if the
-- column already exists, and these need ON DELETE SET NULL (see the
-- fk_votes_*/fk_meeting_actions_* block below) so re-ingest's
-- delete_structured_rows (which deletes parent resolution/meeting rows before
-- re-inserting) doesn't hit an FK violation.
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS resolution_id  INTEGER;
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS meeting_id     INTEGER;
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS meeting_id     INTEGER;
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS resolution_id  INTEGER;
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS legislation_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_votes_resolution_id       ON votes(resolution_id);
CREATE INDEX IF NOT EXISTS idx_votes_meeting_id          ON votes(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_meeting   ON meeting_actions(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_res       ON meeting_actions(resolution_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_leg       ON meeting_actions(legislation_id);

-- ---- vacancies provenance parity: gains source_file ------------------------
ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS source_file VARCHAR(255);

-- ---- meeting_attendance junction -------------------------------------------
CREATE TABLE IF NOT EXISTS meeting_attendance (
    id         SERIAL PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    member_id  INTEGER NOT NULL REFERENCES council_members(id),
    status     VARCHAR(10) NOT NULL CHECK (status IN ('present','absent')),
    UNIQUE (meeting_id, member_id)
);

-- ---- document_type_registry becomes a real FK target -----------------------
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_doc_type_registry_name') THEN
    ALTER TABLE document_type_registry ADD CONSTRAINT uq_doc_type_registry_name UNIQUE (type_name);
  END IF;
END $$;

-- ---- Pure-SQL legislative backfill (earliest-match; ambiguities logged) -----
UPDATE votes v SET resolution_id = r.id
FROM (SELECT resolution_number, MIN(id) AS id FROM resolutions
      WHERE resolution_number IS NOT NULL GROUP BY resolution_number) r
WHERE v.resolution_number = r.resolution_number AND v.resolution_id IS NULL;

UPDATE votes v SET meeting_id = m.id
FROM (SELECT source_file, MIN(id) AS id FROM meetings
      WHERE source_file IS NOT NULL GROUP BY source_file) m
WHERE v.source_file = m.source_file AND v.meeting_id IS NULL;

UPDATE meeting_actions a SET meeting_id = m.id
FROM (SELECT meeting_date, MIN(id) AS id FROM meetings
      WHERE meeting_date IS NOT NULL GROUP BY meeting_date) m
WHERE a.meeting_date = m.meeting_date AND a.meeting_id IS NULL;

UPDATE meeting_actions a SET resolution_id = r.id
FROM (SELECT resolution_number, MIN(id) AS id FROM resolutions
      WHERE resolution_number IS NOT NULL GROUP BY resolution_number) r
WHERE a.item_type = 'resolution' AND a.item_number = r.resolution_number
  AND a.resolution_id IS NULL;

UPDATE meeting_actions a SET legislation_id = l.id
FROM (SELECT bill_number, MIN(id) AS id FROM legislation
      WHERE bill_number IS NOT NULL GROUP BY bill_number) l
WHERE a.item_type ILIKE 'ordinance' AND a.item_number = l.bill_number
  AND a.legislation_id IS NULL;

-- Log earliest-match ambiguities. Idempotent: clear prior migration flags first
-- so re-running yields a stable set instead of accumulating duplicates.
DELETE FROM review_flags WHERE source_file='schema-migration' AND reason LIKE 'ambiguous_%';
-- resolution_numbers that appear on >1 resolution row (ambiguous links).
INSERT INTO review_flags (source_file, stage, reason, detail)
SELECT 'schema-migration', 'validate', 'ambiguous_resolution_number',
       'resolution_number ' || resolution_number || ' maps to ' || COUNT(*) || ' rows'
FROM resolutions WHERE resolution_number IS NOT NULL
GROUP BY resolution_number HAVING COUNT(*) > 1;
-- meeting_dates that appear on >1 meeting row (ambiguous links).
INSERT INTO review_flags (source_file, stage, reason, detail)
SELECT 'schema-migration', 'validate', 'ambiguous_meeting_date',
       'meeting_date ' || meeting_date || ' maps to ' || COUNT(*) || ' rows'
FROM meetings WHERE meeting_date IS NOT NULL
GROUP BY meeting_date HAVING COUNT(*) > 1;
-- bill_numbers that appear on >1 legislation row (ambiguous links).
INSERT INTO review_flags (source_file, stage, reason, detail)
SELECT 'schema-migration', 'validate', 'ambiguous_bill_number',
       'bill_number ' || bill_number || ' maps to ' || COUNT(*) || ' rows'
FROM legislation WHERE bill_number IS NOT NULL
GROUP BY bill_number HAVING COUNT(*) > 1;

-- ---- Provenance foreign keys, NOT VALID (won't fail on legacy orphans) ------
-- source_file -> documents(source_file); ON DELETE SET NULL.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','resolutions','votes','meetings',
    'meeting_actions','legislation','appropriations','goals'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_'||t||'_source_file') THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT fk_%s_source_file FOREIGN KEY (source_file) '
        'REFERENCES documents(source_file) ON DELETE SET NULL NOT VALID', t, t);
    END IF;
  END LOOP;
END $$;

-- source_chunk_id -> document_chunks(chunk_id); ON DELETE SET NULL.
-- Guarded: document_chunks is app-created and may not exist at SQL-bootstrap time.
DO $$
DECLARE t TEXT;
BEGIN
  IF to_regclass('document_chunks') IS NOT NULL THEN
    FOREACH t IN ARRAY ARRAY[
      'expenditures','metrics','grants','vacancies','resolutions','votes','meetings',
      'meeting_actions','legislation','appropriations','goals'
    ] LOOP
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_'||t||'_source_chunk') THEN
        EXECUTE format(
          'ALTER TABLE %I ADD CONSTRAINT fk_%s_source_chunk FOREIGN KEY (source_chunk_id) '
          'REFERENCES document_chunks(chunk_id) ON DELETE SET NULL NOT VALID', t, t);
      END IF;
    END LOOP;
  END IF;
END $$;

-- chunk_performance -> document_chunks (CASCADE); document_chunks -> documents;
-- document_type FKs -> registry (both NOT VALID / SET NULL). All document_chunks
-- refs are guarded on the app-created table; documents -> registry is not.
DO $$ BEGIN
  IF to_regclass('document_chunks') IS NOT NULL THEN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_chunk_perf_chunk') THEN
      ALTER TABLE chunk_performance ADD CONSTRAINT fk_chunk_perf_chunk
        FOREIGN KEY (chunk_id) REFERENCES document_chunks(chunk_id) ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_chunks_doc') THEN
      ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_doc
        FOREIGN KEY (source_file) REFERENCES documents(source_file) ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_chunks_doctype') THEN
      ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_doctype
        FOREIGN KEY (document_type) REFERENCES document_type_registry(type_name)
        ON UPDATE CASCADE ON DELETE SET NULL NOT VALID;
    END IF;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_documents_doctype') THEN
    ALTER TABLE documents ADD CONSTRAINT fk_documents_doctype
      FOREIGN KEY (document_type) REFERENCES document_type_registry(type_name)
      ON UPDATE CASCADE ON DELETE SET NULL NOT VALID;
  END IF;
END $$;

-- ---- Legislative surrogate FKs, NOT VALID (won't fail on legacy orphans) ----
-- votes/meeting_actions -> resolutions/meetings/legislation; ON DELETE SET NULL
-- so re-ingest's delete_structured_rows (which deletes parent resolution/meeting
-- rows before re-inserting) doesn't hit an FK violation on the surviving votes /
-- meeting_actions rows. resolutions/meetings/legislation are base-schema tables
-- (no to_regclass guard needed, unlike the document_chunks-targeting blocks above).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_votes_resolution') THEN
    ALTER TABLE votes ADD CONSTRAINT fk_votes_resolution
      FOREIGN KEY (resolution_id) REFERENCES resolutions(id) ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_votes_meeting') THEN
    ALTER TABLE votes ADD CONSTRAINT fk_votes_meeting
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_meeting_actions_meeting') THEN
    ALTER TABLE meeting_actions ADD CONSTRAINT fk_meeting_actions_meeting
      FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_meeting_actions_resolution') THEN
    ALTER TABLE meeting_actions ADD CONSTRAINT fk_meeting_actions_resolution
      FOREIGN KEY (resolution_id) REFERENCES resolutions(id) ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_meeting_actions_legislation') THEN
    ALTER TABLE meeting_actions ADD CONSTRAINT fk_meeting_actions_legislation
      FOREIGN KEY (legislation_id) REFERENCES legislation(id) ON DELETE SET NULL NOT VALID;
  END IF;
END $$;

-- Legislative status tracking (see migrate_2026_07_29_legislative_status.sql)

CREATE TABLE IF NOT EXISTS legislative_items (
    id             SERIAL PRIMARY KEY,
    item_kind      VARCHAR(20) NOT NULL CHECK (item_kind IN ('resolution','ordinance')),
    item_key       VARCHAR(50) NOT NULL,   -- canonical: "Bill 3-2026" -> "3-2026"
    display_number VARCHAR(50) NOT NULL,   -- first-seen raw form
    title          TEXT,                   -- longest non-empty title seen
    first_seen     DATE,                   -- earliest event date
    UNIQUE (item_kind, item_key)
);

CREATE TABLE IF NOT EXISTS legislative_events (
    id                  SERIAL PRIMARY KEY,
    legislative_item_id INTEGER NOT NULL REFERENCES legislative_items(id) ON DELETE CASCADE,
    event               VARCHAR(30) NOT NULL CHECK (event IN (
        'introduced','first_reading','referred_to_committee','reported_out',
        'amended','passed','tabled','defeated','withdrawn',
        'signed','vetoed','veto_overridden')),
    event_date          DATE,
    seq                 SMALLINT NOT NULL DEFAULT 0,
    origin              VARCHAR(10) NOT NULL CHECK (origin IN ('minutes','document')),
    meeting_action_id   INTEGER REFERENCES meeting_actions(id) ON DELETE CASCADE,
    committee           VARCHAR(120),
    ayes                SMALLINT,
    nays                SMALLINT,
    note                TEXT,
    source_chunk_id     UUID,
    source_file         VARCHAR(255),
    UNIQUE (meeting_action_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_legis_events_item ON legislative_events(legislative_item_id, event_date);

ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE resolutions     ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE legislation     ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_meeting_actions_legis_item ON meeting_actions(legislative_item_id);
CREATE INDEX IF NOT EXISTS idx_votes_legis_item ON votes(legislative_item_id);

-- Current stage per item: max event rank; deliberately date-independent so a
-- noisy OCR date can never demote a signed bill. Ranks per spec section 3.4.
CREATE OR REPLACE VIEW legislative_item_status AS
WITH ranked AS (
    SELECT ev.*, CASE ev.event
        WHEN 'introduced'            THEN 1
        WHEN 'first_reading'         THEN 1
        WHEN 'amended'               THEN 1
        WHEN 'referred_to_committee' THEN 2
        WHEN 'reported_out'          THEN 3
        WHEN 'tabled'                THEN 4
        WHEN 'withdrawn'             THEN 5
        WHEN 'defeated'              THEN 5
        WHEN 'passed'                THEN 6
        WHEN 'signed'                THEN 7
        WHEN 'vetoed'                THEN 7
        WHEN 'veto_overridden'       THEN 8
    END AS rank
    FROM legislative_events ev
), top AS (
    SELECT DISTINCT ON (legislative_item_id)
           legislative_item_id, event, event_date, rank
    FROM ranked
    ORDER BY legislative_item_id, rank DESC, event_date DESC NULLS LAST, id DESC
)
SELECT li.id AS legislative_item_id,
       li.item_kind, li.item_key, li.display_number, li.title,
       CASE top.event
           WHEN 'introduced'            THEN 'introduced'
           WHEN 'first_reading'         THEN 'introduced'
           WHEN 'amended'               THEN 'introduced'
           WHEN 'referred_to_committee' THEN 'in_committee'
           WHEN 'reported_out'          THEN 'awaiting_final_vote'
           WHEN 'tabled'                THEN 'tabled'
           WHEN 'withdrawn'             THEN 'withdrawn'
           WHEN 'defeated'              THEN 'failed'
           WHEN 'passed'                THEN 'passed'
           WHEN 'signed'                THEN 'signed'
           WHEN 'vetoed'                THEN 'vetoed'
           WHEN 'veto_overridden'       THEN 'veto_overridden'
       END AS current_stage,
       top.event_date AS stage_date,
       (CURRENT_DATE - top.event_date) AS days_in_stage,
       agg.first_event_date, agg.last_event_date, agg.meetings_touched,
       agg.latest_committee, agg.passed_ayes, agg.passed_nays
FROM legislative_items li
JOIN top ON top.legislative_item_id = li.id
LEFT JOIN LATERAL (
    SELECT MIN(e.event_date)  AS first_event_date,
           MAX(e.event_date)  AS last_event_date,
           COUNT(DISTINCT e.event_date) FILTER (WHERE e.origin = 'minutes') AS meetings_touched,
           (ARRAY_AGG(e.committee ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE COALESCE(e.committee,'') <> ''))[1] AS latest_committee,
           (ARRAY_AGG(e.ayes ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE e.event = 'passed' AND e.ayes IS NOT NULL))[1] AS passed_ayes,
           (ARRAY_AGG(e.nays ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE e.event = 'passed' AND e.nays IS NOT NULL))[1] AS passed_nays
    FROM legislative_events e
    WHERE e.legislative_item_id = li.id
) agg ON TRUE;

-- Ordered per-meeting history for one item ("what happened at each meeting").
CREATE OR REPLACE VIEW legislative_item_trail AS
SELECT li.id AS legislative_item_id,
       li.item_kind, li.item_key, li.display_number, li.title,
       ev.event, ev.event_date, ev.seq, ev.origin, ev.committee,
       ev.ayes, ev.nays, ev.note, ev.source_file
FROM legislative_items li
JOIN legislative_events ev ON ev.legislative_item_id = li.id
ORDER BY li.item_kind, li.item_key, ev.event_date NULLS LAST, ev.id;
