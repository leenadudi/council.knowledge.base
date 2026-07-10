-- Guardrails migration (2026-07-09). Idempotent; safe to re-run.
ALTER TABLE votes ALTER COLUMN vote TYPE VARCHAR(50);

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
