-- sql/seed_harrisburg_department_aliases.sql
-- HARRISBURG-ONLY. Do NOT run for any other city. Replays the known-good merges
-- already curated in dashboard/aggregator._DEPT_ALIASES so Harrisburg's dimension
-- doesn't fragment on variants the generic rules can't catch. Run AFTER the
-- backfill (so the canonical departments exist). Idempotent.
-- Each pair: (raw alias seen in data)  ->  (canonical_key of the target dept).
INSERT INTO department_aliases (department_id, alias)
SELECT d.id, v.alias
FROM departments d
JOIN (VALUES
    ('planning',                'Planning Bureau'),
    ('city council',            'Harrisburg City Council'),
    ('city council',            'City of Harrisburg City Council'),
    ('budget & finance',        'Department of Finance'),
    ('budget & finance',        'Departnment of Finance'),
    ('parks & recreation',      'Park Maintenance'),
    ('parks & recreation',      'Parks Maintenance'),
    ('building & housing development',
                                'Department of Economic Development & Building and Housing'),
    ('codes',                   'Codes Health Department'),
    ('police',                  'Buraeu of Police'),
    ('communications',          'Bureau of Coummunicaitons')
) AS v(target_key, alias) ON d.canonical_key = v.target_key
ON CONFLICT (alias) DO NOTHING;
