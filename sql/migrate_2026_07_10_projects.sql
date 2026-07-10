-- Projects table migration (2026-07-10). Idempotent; safe to re-run.
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
