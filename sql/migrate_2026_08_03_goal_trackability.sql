-- 2026-08-03 Goal trackability (additive).
-- AI-suggested + human-confirmed classification of goals into
-- measurable / milestone / aspiration (needs_review = low-confidence triage).
-- Design: docs/superpowers/specs/2026-07-29-goal-trackability-design.md
-- Written group-wide per logical goal (canonical department + normalized title),
-- so all quarterly restatements of one goal carry the same label.

ALTER TABLE goals ADD COLUMN IF NOT EXISTS trackability            TEXT;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS trackability_confidence NUMERIC;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS trackability_rationale  TEXT;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS trackability_metric     TEXT;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS trackability_deadline   TEXT;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS trackability_source     TEXT;  -- 'ai' | 'human'
ALTER TABLE goals ADD COLUMN IF NOT EXISTS trackability_at         TIMESTAMP;
