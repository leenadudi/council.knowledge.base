-- 2026-07-29: indexes backing the year-to-date report's date-bucketed scans.
CREATE INDEX IF NOT EXISTS idx_resolutions_adopted ON resolutions(adopted_date);
CREATE INDEX IF NOT EXISTS idx_legislation_adopted ON legislation(adopted_date);
CREATE INDEX IF NOT EXISTS idx_grants_start ON grants(start_date);
