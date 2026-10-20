# eCode360 Source Links on Citations — Design

**Date:** 2026-07-10
**Status:** Approved for planning

## Problem

Query answers cite documents by `source_file` (e.g.
`"Misc. Documents - Quarterly Reports - 2025 - Bureau of Police_Q1 2025.pdf"`),
but give the user no way to open the underlying document. eCode360 hosts the
originals, and we do **not** have API access.

## Goal

Attach a clickable eCode360 URL to every citation that points the user to the
**folder** containing the source document — one click from the file — derived
deterministically from `source_file`, with no API access, no scraping, no
re-ingestion, and no LLM spend.

Non-goal: linking to the exact document. That requires eCode360's opaque
numeric asset ID (e.g. `753199897`), which we do not store for the 120
human-named documents and cannot recover without re-downloading. Deferred.

## Key insight

The stored filenames already encode the eCode360 document-browser hierarchy,
with ` - ` as the folder separator:

```
Misc. Documents - Quarterly Reports - 2025 - Bureau of Police_Q1 2025.pdf
└─ folder ────┘   └─ subfolder ───┘   └yr┘   └─ document name ─────────┘
```

Segment-count analysis of the 120 human-named documents:

| Segments (split on ` - `) | Count |
|---|---|
| 4 | 92 |
| 5 | 20 |
| 6 | 8 |

The variance lives entirely in the **document name**, which can contain its own
` - ` (e.g. *"Resolution 5-2026 - 2601 North 3rd Street"*). The folder path is
**always the first 3 segments** (category / subfolder / year). Because we link
to the folder and drop the document name, that variance does not affect us.

A user-confirmed live URL establishes the encoding baseline:
`https://ecode360.com/HA1391/documents/Misc._Documents` — spaces become
underscores, the `.` in `Misc.` is preserved.

## Design

### Component: `src/query/source_links.py` (new)

A single pure function. No network, no I/O, no LLM. Fully unit-testable offline.

```python
def build_ecode_url(source_file: str, town_code: str = "HA1391") -> str | None:
    """Build an eCode360 folder-level URL from a stored source filename.

    Returns None when the filename cannot be mapped to a folder path
    (fewer than 3 folder segments — e.g. numeric-named asset files).
    """
```

Algorithm:

1. Strip the extension (`.pdf`, `.html`).
2. Strip trailing dedup artifacts: ` (1)`, ` (2)`, … (download duplicates).
3. Split on ` - `.
4. If fewer than 4 segments total (i.e. no room for 3 folder segments + a
   name) → return `None`. This covers the 11 numeric `753…pdf` files and any
   malformed name. Caller omits the URL rather than emit a broken link.
5. Take the **first 3 segments** as the folder path; discard segment 4+.
6. Encode each folder segment: spaces → `_`; all other characters left
   literal (best-guess baseline — see Verification).
7. Assemble:
   `https://ecode360.com/{town_code}/documents/{seg1}/{seg2}/{seg3}`

### Wiring

- `src/models.py` — add `source_url: Optional[str] = None` to `Citation`.
  Optional with a default; nothing that constructs a `Citation` today breaks.
- `src/query/synthesizer.py` (`_format_context`, ~line 110) — call
  `build_ecode_url(source)` and pass the result to `Citation(source_url=...)`.

### Multi-city

`town_code` is a parameter defaulting to `"HA1391"` (Harrisburg), not a
hardcoded literal — aligns with the multi-city roadmap. Sourced from config
when the caller has a town context; defaulted otherwise.

## Verification (manual, browser)

eCode360 is behind Cloudflare bot protection — every automated request returns
403, so constructed URLs cannot be validated in code. A human browser can.

The encoding rules for special characters are therefore unconfirmed:
- `&` in *"Department of Budget & Finance"* — literal, `and`, or `%26`?
- `.` in *"Misc."* — preserved (baseline assumes yes).
- `(` `)`, apostrophes, `,`.

Before trusting the rules, generate ~6 representative URLs — one per document
category (Misc. Documents, Budgets, Legislation, Resolutions, Minutes) plus the
`&`-department edge case — and click-test each in a browser. Lock the encoding
in `build_ecode_url` to whatever actually resolves. Until then the function
ships with the baseline rules (spaces → `_`, everything else literal).

## Testing

Unit tests for `build_ecode_url` (offline, no network, no LLM):

- 4-segment name → 3-segment folder URL
- 5- and 6-segment names → same 3-segment folder URL (name variance ignored)
- ` (1)` dedup suffix stripped
- `&`-department name (asserts the chosen encoding)
- numeric-named file (`753199897.pdf`) → `None`
- extension stripping (`.pdf`, `.html`)
- custom `town_code` honored

## YAGNI cuts

- No exact-document / asset-ID linking.
- No scraping or download of eCode360.
- No in-code URL validation (impossible through Cloudflare).
- No caching (function is trivial).

## Impact on in-flight work

None. This is query-side only. It shares no files, tables, or migrations with
the concurrent ingestion/extraction (vacancy) work, requires no re-ingestion,
and spends no LLM budget.
