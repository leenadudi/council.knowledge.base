-- Data-driven document types need a profiler-facing description (the DocumentType.description
-- that populates the profiler's type menu). document_type_registry only had display_name.
-- Idempotent.
ALTER TABLE document_type_registry ADD COLUMN IF NOT EXISTS description TEXT;
