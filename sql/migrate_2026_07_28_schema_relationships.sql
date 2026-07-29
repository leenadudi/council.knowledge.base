-- sql/migrate_2026_07_28_schema_relationships.sql
-- Additive, idempotent. Declares real FKs + three self-populating entity
-- dimensions. Ships all dimension tables EMPTY (populated by scripts/backfill_
-- entities_2026_07_28.py). Safe to re-run.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---- Entity dimensions (created empty; no seed rows) -----------------------
CREATE TABLE IF NOT EXISTS departments (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,
    canonical_key  VARCHAR(120) NOT NULL UNIQUE,
    dept_type      VARCHAR(30),
    parent_id      INTEGER REFERENCES departments(id),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS department_aliases (
    id            SERIAL PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    alias         VARCHAR(150) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_department_aliases_alias ON department_aliases(LOWER(alias));

CREATE TABLE IF NOT EXISTS council_members (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(150) NOT NULL UNIQUE,
    canonical_key  VARCHAR(150) NOT NULL UNIQUE,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS council_member_aliases (
    id        SERIAL PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES council_members(id) ON DELETE CASCADE,
    alias     VARCHAR(150) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_member_aliases_alias ON council_member_aliases(LOWER(alias));

CREATE TABLE IF NOT EXISTS vendors (
    id             SERIAL PRIMARY KEY,
    canonical_name VARCHAR(200) NOT NULL UNIQUE,
    canonical_key  VARCHAR(200) NOT NULL UNIQUE,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS vendor_aliases (
    id        SERIAL PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    alias     VARCHAR(200) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_vendor_aliases_alias ON vendor_aliases(LOWER(alias));

-- ---- department_id on the 10 department-bearing tables ---------------------
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','documents','resolutions',
    'appropriations','goals','document_chunks','evaluation_suite'
  ] LOOP
    EXECUTE format(
      'ALTER TABLE %I ADD COLUMN IF NOT EXISTS department_id INTEGER REFERENCES departments(id)', t);
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS idx_%s_department_id ON %I(department_id)', t, t);
  END LOOP;
END $$;

-- ---- people + vendor FK columns on the tables that carry those strings -----
ALTER TABLE votes       ADD COLUMN IF NOT EXISTS member_id    INTEGER REFERENCES council_members(id);
ALTER TABLE meetings    ADD COLUMN IF NOT EXISTS president_id INTEGER REFERENCES council_members(id);
ALTER TABLE legislation ADD COLUMN IF NOT EXISTS sponsor_id   INTEGER REFERENCES council_members(id);
ALTER TABLE resolutions ADD COLUMN IF NOT EXISTS vendor_id    INTEGER REFERENCES vendors(id);
CREATE INDEX IF NOT EXISTS idx_votes_member_id      ON votes(member_id);
CREATE INDEX IF NOT EXISTS idx_meetings_president_id ON meetings(president_id);
CREATE INDEX IF NOT EXISTS idx_legislation_sponsor  ON legislation(sponsor_id);
CREATE INDEX IF NOT EXISTS idx_resolutions_vendor_id ON resolutions(vendor_id);

-- ---- legislative surrogate-FK columns --------------------------------------
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS resolution_id  INTEGER REFERENCES resolutions(id);
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS meeting_id     INTEGER REFERENCES meetings(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS meeting_id     INTEGER REFERENCES meetings(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS resolution_id  INTEGER REFERENCES resolutions(id);
ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS legislation_id INTEGER REFERENCES legislation(id);
CREATE INDEX IF NOT EXISTS idx_votes_resolution_id       ON votes(resolution_id);
CREATE INDEX IF NOT EXISTS idx_votes_meeting_id          ON votes(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_meeting   ON meeting_actions(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_res       ON meeting_actions(resolution_id);
CREATE INDEX IF NOT EXISTS idx_meeting_actions_leg       ON meeting_actions(legislation_id);

-- ---- vacancies provenance parity: gains source_file ------------------------
ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS source_file VARCHAR(255);

-- ---- meeting_attendance junction -------------------------------------------
CREATE TABLE IF NOT EXISTS meeting_attendance (
    id         SERIAL PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    member_id  INTEGER NOT NULL REFERENCES council_members(id),
    status     VARCHAR(10) NOT NULL CHECK (status IN ('present','absent')),
    UNIQUE (meeting_id, member_id)
);

-- ---- document_type_registry becomes a real FK target -----------------------
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_doc_type_registry_name') THEN
    ALTER TABLE document_type_registry ADD CONSTRAINT uq_doc_type_registry_name UNIQUE (type_name);
  END IF;
END $$;

-- ---- Pure-SQL legislative backfill (earliest-match; ambiguities logged) -----
UPDATE votes v SET resolution_id = r.id
FROM (SELECT resolution_number, MIN(id) AS id FROM resolutions
      WHERE resolution_number IS NOT NULL GROUP BY resolution_number) r
WHERE v.resolution_number = r.resolution_number AND v.resolution_id IS NULL;

UPDATE votes v SET meeting_id = m.id
FROM (SELECT source_file, MIN(id) AS id FROM meetings
      WHERE source_file IS NOT NULL GROUP BY source_file) m
WHERE v.source_file = m.source_file AND v.meeting_id IS NULL;

UPDATE meeting_actions a SET meeting_id = m.id
FROM (SELECT meeting_date, MIN(id) AS id FROM meetings
      WHERE meeting_date IS NOT NULL GROUP BY meeting_date) m
WHERE a.meeting_date = m.meeting_date AND a.meeting_id IS NULL;

UPDATE meeting_actions a SET resolution_id = r.id
FROM (SELECT resolution_number, MIN(id) AS id FROM resolutions
      WHERE resolution_number IS NOT NULL GROUP BY resolution_number) r
WHERE a.item_type = 'resolution' AND a.item_number = r.resolution_number
  AND a.resolution_id IS NULL;

UPDATE meeting_actions a SET legislation_id = l.id
FROM (SELECT bill_number, MIN(id) AS id FROM legislation
      WHERE bill_number IS NOT NULL GROUP BY bill_number) l
WHERE a.item_type ILIKE 'ordinance' AND a.item_number = l.bill_number
  AND a.legislation_id IS NULL;

-- Log resolution_numbers that appear on >1 resolution row (ambiguous links).
INSERT INTO review_flags (source_file, stage, reason, detail)
SELECT 'schema-migration', 'validate', 'ambiguous_resolution_number',
       'resolution_number ' || resolution_number || ' maps to ' || COUNT(*) || ' rows'
FROM resolutions WHERE resolution_number IS NOT NULL
GROUP BY resolution_number HAVING COUNT(*) > 1;
-- Same pattern applies to duplicate meeting_date and bill_number; add analogous
-- INSERT...SELECT...HAVING COUNT(*)>1 blocks for meetings(meeting_date) and
-- legislation(bill_number) so every earliest-match ambiguity is visible.

-- ---- Provenance foreign keys, NOT VALID (won't fail on legacy orphans) ------
-- source_file -> documents(source_file); ON DELETE SET NULL.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','resolutions','votes','meetings',
    'meeting_actions','legislation','appropriations','goals','document_chunks'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_'||t||'_source_file') THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT fk_%s_source_file FOREIGN KEY (source_file) '
        'REFERENCES documents(source_file) ON DELETE SET NULL NOT VALID', t, t);
    END IF;
  END LOOP;
END $$;

-- source_chunk_id -> document_chunks(chunk_id); ON DELETE SET NULL.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'expenditures','metrics','grants','vacancies','resolutions','votes','meetings',
    'meeting_actions','legislation','appropriations','goals'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_'||t||'_source_chunk') THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT fk_%s_source_chunk FOREIGN KEY (source_chunk_id) '
        'REFERENCES document_chunks(chunk_id) ON DELETE SET NULL NOT VALID', t, t);
    END IF;
  END LOOP;
END $$;

-- chunk_performance -> document_chunks (CASCADE); document_chunks -> documents;
-- document_type FKs -> registry (both NOT VALID / SET NULL).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_chunk_perf_chunk') THEN
    ALTER TABLE chunk_performance ADD CONSTRAINT fk_chunk_perf_chunk
      FOREIGN KEY (chunk_id) REFERENCES document_chunks(chunk_id) ON DELETE CASCADE NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_chunks_doc') THEN
    ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_doc
      FOREIGN KEY (source_file) REFERENCES documents(source_file) ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_documents_doctype') THEN
    ALTER TABLE documents ADD CONSTRAINT fk_documents_doctype
      FOREIGN KEY (document_type) REFERENCES document_type_registry(type_name)
      ON UPDATE CASCADE ON DELETE SET NULL NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_chunks_doctype') THEN
    ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_doctype
      FOREIGN KEY (document_type) REFERENCES document_type_registry(type_name)
      ON UPDATE CASCADE ON DELETE SET NULL NOT VALID;
  END IF;
END $$;
