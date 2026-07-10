-- Guardrails migration (2026-07-09). Idempotent; safe to re-run.
ALTER TABLE votes ALTER COLUMN vote TYPE VARCHAR(50);
