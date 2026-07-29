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
    ingested_at     TIMESTAMP DEFAULT NOW()
);

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
    source_chunk_id    UUID,
    source_file        VARCHAR(255),
    ingested_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_goals_dept ON goals(department, year);

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
