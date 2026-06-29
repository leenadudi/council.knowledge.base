# Golden-Set Fixtures

This directory holds real document examples used by the **hybrid golden-set test harness**
(`tests/ingestion/test_golden_set.py`).

Each case consists of exactly two files placed in the same sub-directory:

| File | Purpose |
|------|---------|
| `<name>.txt` (or `<name>.pdf`) | The raw source document to ingest |
| `<name>.expected.json` | Contract: document type, hard facts, judge guidance |

The test harness globs `tests/fixtures/**/*.expected.json` recursively.
With no files present, the parametrized test is skipped cleanly.

---

## How to add a fixture

1. **Choose a sub-directory** that matches the document type, e.g.
   `tests/fixtures/resolution/` or `tests/fixtures/quarterly_report/`.

2. **Drop the source document** as `<name>.txt` (plain text) or `<name>.pdf`.
   Plain text is preferred because it avoids parser variation between runs.

3. **Create `<name>.expected.json`** with this structure:

```json
{
  "source_text_file": "<name>.txt",
  "document_type": "<registered type name>",
  "hard_facts": {
    "<dot-bracket path>": <exact scalar value>,
    ...
  },
  "judge_notes": "Free-text hint to the LLM judge about what to look for."
}
```

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `source_text_file` | string | Filename of the companion `.txt`/`.pdf` (same directory) |
| `document_type` | string | Must be a registered type name (see `src/ingestion/registry.py`) |
| `hard_facts` | object | Dot-and-bracket paths into `extract_for_type()` output; values are exact scalar checks |
| `judge_notes` | string | Guidance for the LLM judge (what to look for, what matters) |

### Path syntax for `hard_facts`

Paths use dot notation for keys and `[N]` for list indices:

```
"resolutions[0].resolution_number"   → extracted["resolutions"][0]["resolution_number"]
"resolutions[0].amount"              → extracted["resolutions"][0]["amount"]
"votes[0].vote"                      → extracted["votes"][0]["vote"]
```

---

## Example (template — NOT a live fixture)

The file below is shown only as a template.
**Do not create `2026-R-12.expected.json` here until you also have the matching `2026-R-12.txt`**,
and the LLM can reliably reproduce the `hard_facts` you assert.

```json
{
  "source_text_file": "2026-R-12.txt",
  "document_type": "resolution",
  "hard_facts": {
    "resolutions[0].resolution_number": "2026-R-12",
    "resolutions[0].amount": 40000.0,
    "resolutions[0].adopted_date": "2026-03-03"
  },
  "judge_notes": "Should capture the $40,000 award to the vendor and the council vote tally."
}
```

The companion `2026-R-12.txt` would contain the full resolution text.

---

## Running the tests

```bash
# Run only the golden-set integration cases (requires ANTHROPIC_API_KEY):
pytest tests/ingestion/test_golden_set.py -m integration -v

# Run all non-integration tests (no API key needed — golden set skipped cleanly):
pytest -q -m "not integration"

# Confirm collection is clean with zero live fixtures:
pytest tests/ingestion/test_golden_set.py --collect-only
```
