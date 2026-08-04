-- Queue of agent-proposed structured-data types/mappings awaiting human review.
-- Written by the ingest-side triage agent for unclassified documents; read (M1) and
-- acted on (M3) from the dashboard. Idempotent.
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
