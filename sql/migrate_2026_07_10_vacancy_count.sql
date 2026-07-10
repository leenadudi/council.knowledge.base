-- Vacancy count migration (2026-07-10). Idempotent; safe to re-run.
-- Adds the open-position count that reports state (e.g. "Patrol Officer- (25)"),
-- which the position-title-only schema previously discarded.
ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS open_count INTEGER;
