-- Legislative status tracking (2026-07-29). Idempotent; safe to re-run.
-- Spec: docs/superpowers/specs/2026-07-29-legislative-status-tracking-design.md

CREATE TABLE IF NOT EXISTS legislative_items (
    id             SERIAL PRIMARY KEY,
    item_kind      VARCHAR(20) NOT NULL CHECK (item_kind IN ('resolution','ordinance')),
    item_key       VARCHAR(50) NOT NULL,   -- canonical: "Bill 3-2026" -> "3-2026"
    display_number VARCHAR(50) NOT NULL,   -- first-seen raw form
    title          TEXT,                   -- longest non-empty title seen
    first_seen     DATE,                   -- earliest event date
    UNIQUE (item_kind, item_key)
);

CREATE TABLE IF NOT EXISTS legislative_events (
    id                  SERIAL PRIMARY KEY,
    legislative_item_id INTEGER NOT NULL REFERENCES legislative_items(id) ON DELETE CASCADE,
    event               VARCHAR(30) NOT NULL CHECK (event IN (
        'introduced','first_reading','referred_to_committee','reported_out',
        'amended','passed','tabled','defeated','withdrawn',
        'signed','vetoed','veto_overridden')),
    event_date          DATE,
    seq                 SMALLINT NOT NULL DEFAULT 0,
    origin              VARCHAR(10) NOT NULL CHECK (origin IN ('minutes','document')),
    meeting_action_id   INTEGER REFERENCES meeting_actions(id) ON DELETE CASCADE,
    committee           VARCHAR(120),
    ayes                SMALLINT,
    nays                SMALLINT,
    note                TEXT,
    source_chunk_id     UUID,
    source_file         VARCHAR(255),
    UNIQUE (meeting_action_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_legis_events_item ON legislative_events(legislative_item_id, event_date);

ALTER TABLE meeting_actions ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE resolutions     ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE legislation     ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
ALTER TABLE votes           ADD COLUMN IF NOT EXISTS legislative_item_id INTEGER REFERENCES legislative_items(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_meeting_actions_legis_item ON meeting_actions(legislative_item_id);
CREATE INDEX IF NOT EXISTS idx_votes_legis_item ON votes(legislative_item_id);

-- Current stage per item: max event rank; deliberately date-independent so a
-- noisy OCR date can never demote a signed bill. Ranks per spec section 3.4.
CREATE OR REPLACE VIEW legislative_item_status AS
WITH ranked AS (
    SELECT ev.*, CASE ev.event
        WHEN 'introduced'            THEN 1
        WHEN 'first_reading'         THEN 1
        WHEN 'amended'               THEN 1
        WHEN 'referred_to_committee' THEN 2
        WHEN 'reported_out'          THEN 3
        WHEN 'tabled'                THEN 4
        WHEN 'withdrawn'             THEN 5
        WHEN 'defeated'              THEN 5
        WHEN 'passed'                THEN 6
        WHEN 'signed'                THEN 7
        WHEN 'vetoed'                THEN 7
        WHEN 'veto_overridden'       THEN 8
    END AS rank
    FROM legislative_events ev
), top AS (
    SELECT DISTINCT ON (legislative_item_id)
           legislative_item_id, event, event_date, rank
    FROM ranked
    ORDER BY legislative_item_id, rank DESC, event_date DESC NULLS LAST, id DESC
)
SELECT li.id AS legislative_item_id,
       li.item_kind, li.item_key, li.display_number, li.title,
       CASE top.event
           WHEN 'introduced'            THEN 'introduced'
           WHEN 'first_reading'         THEN 'introduced'
           WHEN 'amended'               THEN 'introduced'
           WHEN 'referred_to_committee' THEN 'in_committee'
           WHEN 'reported_out'          THEN 'awaiting_final_vote'
           WHEN 'tabled'                THEN 'tabled'
           WHEN 'withdrawn'             THEN 'withdrawn'
           WHEN 'defeated'              THEN 'failed'
           WHEN 'passed'                THEN 'passed'
           WHEN 'signed'                THEN 'signed'
           WHEN 'vetoed'                THEN 'vetoed'
           WHEN 'veto_overridden'       THEN 'veto_overridden'
       END AS current_stage,
       top.event_date AS stage_date,
       (CURRENT_DATE - top.event_date) AS days_in_stage,
       agg.first_event_date, agg.last_event_date, agg.meetings_touched,
       agg.latest_committee, agg.passed_ayes, agg.passed_nays
FROM legislative_items li
JOIN top ON top.legislative_item_id = li.id
LEFT JOIN LATERAL (
    SELECT MIN(e.event_date)  AS first_event_date,
           MAX(e.event_date)  AS last_event_date,
           COUNT(DISTINCT e.event_date) FILTER (WHERE e.origin = 'minutes') AS meetings_touched,
           (ARRAY_AGG(e.committee ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE COALESCE(e.committee,'') <> ''))[1] AS latest_committee,
           (ARRAY_AGG(e.ayes ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE e.event = 'passed' AND e.ayes IS NOT NULL))[1] AS passed_ayes,
           (ARRAY_AGG(e.nays ORDER BY e.event_date DESC NULLS LAST)
              FILTER (WHERE e.event = 'passed' AND e.nays IS NOT NULL))[1] AS passed_nays
    FROM legislative_events e
    WHERE e.legislative_item_id = li.id
) agg ON TRUE;

-- Ordered per-meeting history for one item ("what happened at each meeting").
CREATE OR REPLACE VIEW legislative_item_trail AS
SELECT li.id AS legislative_item_id,
       li.item_kind, li.item_key, li.display_number, li.title,
       ev.event, ev.event_date, ev.seq, ev.origin, ev.committee,
       ev.ayes, ev.nays, ev.note, ev.source_file
FROM legislative_items li
JOIN legislative_events ev ON ev.legislative_item_id = li.id
ORDER BY li.item_kind, li.item_key, ev.event_date NULLS LAST, ev.id;
