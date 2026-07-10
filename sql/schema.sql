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
