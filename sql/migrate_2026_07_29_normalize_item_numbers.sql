-- Canonicalize meeting_actions.item_number to its bare "N-YYYY" form.
-- Minutes extractions were inconsistent per document ("Resolution 24-2026" vs
-- "24-2026" vs "Ordinance Bill 2-2026"), splitting one legislative item into
-- several under any item_number grouping — the dashboard pipeline showed 24
-- items "in committee" when only 6 were (17 phantom referral-only halves).
-- New ingests are normalized in SQLStore.insert_meeting_action_rows; this
-- cleans rows ingested before that fix. Idempotent.
UPDATE meeting_actions
SET item_number = regexp_replace(item_number, '^\s*((resolution|ordinance|bill|no\.?|#)\s+)+', '', 'i')
WHERE item_number ~* '^\s*(resolution|ordinance|bill|no\.?|#)\s+\S'
  AND regexp_replace(item_number, '^\s*((resolution|ordinance|bill|no\.?|#)\s+)+', '', 'i') <> '';
