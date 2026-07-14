#!/usr/bin/env python3
"""Batch-API backfill: re-extract ALL quarterly reports from EXISTING chunks via the
Anthropic Message Batches API (50% cheaper than the synchronous path). Same extraction
schema, dedup, and user_status safeguard as scripts/reextract_quarterly.py — only the
transport differs (async batch instead of per-call). Operator-run (live Supabase +
ANTHROPIC_API_KEY).

Because submitting a batch spends money, the default is a true zero-cost dry-run: it
builds every request and prints the count + an estimated cost, but does NOT submit.

Usage:
  python3 scripts/reextract_quarterly_batch.py            # dry-run: build + estimate, NO submit, NO spend
  python3 scripts/reextract_quarterly_batch.py --write     # submit batch, poll, write results
"""
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from src.config import get_settings
from src.extraction.sql_extractor import SQLExtractor
from src.storage.sql_store import SQLStore
from src.ingestion.schemas.quarterly_report import QuarterlyReportExtraction
from scripts.reextract_quarterly import merge_user_status, _TABLE_INSERT

# claude-sonnet-4-6 list price $3/$15 per 1M tokens; Batch API is 50% off.
_BATCH_IN_PER_TOK = 3.0 / 1_000_000 * 0.5
_BATCH_OUT_PER_TOK = 15.0 / 1_000_000 * 0.5
_POLL_SECONDS = 30


def _build_requests(store, ext, batch_size, limit=None, skip=0):
    """Return (requests, meta). meta[ridx] = per-report info for writing results back.
    custom_id encodes report + chunk-batch index: 'r{ridx}_b{bidx}'.
    Ordering is deterministic, so skip/limit select a stable slice — skip past reports
    already backfilled, limit caps the count (for small tests)."""
    with store.cursor() as cur:
        cur.execute("SELECT DISTINCT source_file, department, quarter, year "
                    "FROM document_chunks WHERE document_type='quarterly_report' "
                    "ORDER BY department, year, quarter")
        reports = [dict(r) for r in cur.fetchall()]
    # Duplicate guard: skip re-uploaded copies like "…Q4 2025 (1).pdf" so the same
    # dept+period isn't double-counted (matches the resolutions re-ingest precedent).
    dup = [r for r in reports if re.search(r" \(\d+\)\.pdf$", r["source_file"])]
    if dup:
        for r in dup:
            print(f"[dup-skip] {r['source_file']}")
        reports = [r for r in reports if r not in dup]
    reports = reports[skip:] if limit is None else reports[skip:skip + limit]

    requests, meta = [], {}
    for ridx, rep in enumerate(reports):
        sf, dept, q, y = rep["source_file"], rep["department"], rep["quarter"], rep["year"]
        with store.cursor() as cur:
            cur.execute("SELECT chunk_id, text FROM document_chunks WHERE source_file=%s ORDER BY chunk_id", (sf,))
            chunk_rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT department, year, quarter, goal_title, user_status, user_status_at "
                        "FROM goals WHERE source_file=%s", (sf,))
            prior_goals = [dict(r) for r in cur.fetchall()]
        texts = [c["text"] for c in chunk_rows]
        meta[ridx] = {"source_file": sf, "department": dept, "quarter": q or "", "year": y,
                      "cid": str(chunk_rows[0]["chunk_id"]) if chunk_rows else None,
                      "prior_goals": prior_goals, "nbatches": 0}
        for bidx in range(0, len(texts), batch_size):
            meta[ridx]["nbatches"] += 1
            prompt = ext.quarterly_prompt(texts[bidx:bidx + batch_size], QuarterlyReportExtraction)
            requests.append(Request(
                custom_id=f"r{ridx}_b{bidx}",
                params=MessageCreateParamsNonStreaming(
                    model=ext.cfg.synthesis_model, max_tokens=16000,  # avoid truncating dense batches
                    messages=[{"role": "user", "content": prompt}]),
            ))
    return requests, meta


def _write_report(store, ext, m, parts):
    """Merge one report's batch parts, preserve goal user_status, and write rows."""
    data = ext.merge_quarterly_parts(parts, m["department"], m["quarter"], m["year"])
    if data.get("goals"):
        data["goals"], unmatched = merge_user_status(m["prior_goals"], data["goals"])
        for u in unmatched:
            print(f"   [user_status] UNMATCHED after re-extract: {u['goal_title']!r} "
                  f"status={u['user_status']} (reconcile manually)")
    counts = {k: len(v) for k, v in data.items()}
    if m["cid"]:
        store.delete_structured_rows(m["source_file"])
        for key, method, has_file in _TABLE_INSERT:
            rows = data.get(key)
            if not rows:
                continue
            (getattr(store, method)(rows, m["cid"], m["source_file"]) if has_file
             else getattr(store, method)(rows, m["cid"]))
    print(f"   {m['source_file']}\n      {m['department']} {m['quarter']} {m['year']} -> {counts}")


def main(write: bool, limit=None, from_batch=None, skip=0):
    cfg = get_settings()
    store = SQLStore(cfg); store.connect()
    ext = SQLExtractor(cfg)  # used only for its stateless prompt/merge helpers
    requests, meta = _build_requests(store, ext, cfg.extraction_batch_size, limit=limit, skip=skip)
    nreq = len(requests)
    if limit is not None or skip:
        print(f"[skip={skip} limit={limit}] reports: " + ", ".join(
            f"{m['department']} {m['quarter']} {m['year']}" for m in meta.values()))
    mode = ("REUSE batch " + from_batch) if from_batch else \
        ("WRITE (submit batch)" if write else "DRY-RUN (no submit, no spend)")
    print(f"Quarterly reports: {len(meta)} | batch requests: {nreq} | mode: {mode}")

    if not write and not from_batch:
        # No LLM call is made in dry-run, so no real token counts. Estimate from the
        # empirical average (~3,384 in / 1,559 out per extraction call, halved for batch).
        est = nreq * (3384 * _BATCH_IN_PER_TOK + 1559 * _BATCH_OUT_PER_TOK)
        print(f"Estimated batch cost (rough, empirical avg): ~${est:,.2f} "
              f"(≈ ${est * 2:,.2f} at synchronous rates)")
        print("Re-run with --write to submit the batch.")
        store.close()
        return

    # Raw Anthropic client (not TrackedAnthropic): batch usage doesn't flow through the
    # llm_usage wrapper — we report cost from the batch's returned usage instead.
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    if from_batch:
        # Recovery/reuse path: collect + write from an already-submitted batch without
        # paying to re-run it (e.g. when a prior run's write step crashed). Report
        # ordering is deterministic, so custom_id r{ridx} still maps to meta[ridx].
        batch_id = from_batch
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status != "ended":
            print(f"batch {batch_id} not ended yet (status={b.processing_status}); try later")
            store.close()
            return
        print(f"Reusing batch {batch_id} (no new submit).")
    else:
        batch = client.messages.batches.create(requests=requests)
        batch_id = batch.id
        print(f"Submitted batch {batch_id}; polling every {_POLL_SECONDS}s (usually < 1h)...")
        while True:
            b = client.messages.batches.retrieve(batch_id)
            if b.processing_status == "ended":
                break
            rc = b.request_counts
            print(f"   status={b.processing_status} processing={rc.processing} "
                  f"succeeded={rc.succeeded} errored={rc.errored}")
            time.sleep(_POLL_SECONDS)

    # Collect results, group per report by custom_id (results arrive unordered).
    parts_by_report = defaultdict(list)
    tok_in = tok_out = errored = 0
    for result in client.messages.batches.results(batch_id):
        ridx = int(result.custom_id.split("_")[0][1:])
        if result.result.type == "succeeded":
            msg = result.result.message
            tok_in += msg.usage.input_tokens
            tok_out += msg.usage.output_tokens
            raw = next((blk.text for blk in msg.content if blk.type == "text"), "")
            parts_by_report[ridx].append(ext.parse_quarterly_response(raw, QuarterlyReportExtraction))
        else:
            errored += 1
            print(f"   [batch] {result.custom_id} {result.result.type}")

    print(f"\nWriting {len(parts_by_report)} reports ({errored} errored requests skipped)...")
    for ridx, parts in sorted(parts_by_report.items()):
        _write_report(store, ext, meta[ridx], parts)

    cost = tok_in * _BATCH_IN_PER_TOK + tok_out * _BATCH_OUT_PER_TOK
    print(f"\nDone. Tokens: {tok_in:,} in / {tok_out:,} out. Actual batch cost: ${cost:,.2f}")
    store.close()


if __name__ == "__main__":
    _limit = None
    if "--limit" in sys.argv:
        _limit = int(sys.argv[sys.argv.index("--limit") + 1])
    _from = None
    if "--from-batch" in sys.argv:
        _from = sys.argv[sys.argv.index("--from-batch") + 1]
    _skip = 0
    if "--skip" in sys.argv:
        _skip = int(sys.argv[sys.argv.index("--skip") + 1])
    main(write="--write" in sys.argv, limit=_limit, from_batch=_from, skip=_skip)
