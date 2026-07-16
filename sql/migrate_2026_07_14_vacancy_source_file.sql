-- Give `vacancies` a source_file column so re-ingest deletes by file like every other
-- structured table. Previously vacancies were deleted via a subquery against
-- document_chunks; but the pipeline deletes the chunks FIRST, so on re-ingest the
-- subquery matched nothing and old vacancy rows were orphaned (duplicated). Idempotent.

ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS source_file VARCHAR(255);

-- Backfill source_file for existing rows from their originating chunk (while chunks
-- still exist). Rows whose chunk is already gone stay NULL and are handled by the old
-- subquery path only until their next clean re-ingest.
UPDATE vacancies v
SET source_file = dc.source_file
FROM document_chunks dc
WHERE v.source_chunk_id = dc.chunk_id
  AND v.source_file IS NULL;

CREATE INDEX IF NOT EXISTS idx_vacancies_source_file ON vacancies(source_file);
