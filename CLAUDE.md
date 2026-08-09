# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Internal decision-support knowledge base for municipal councils/clerks. Flagship data set is the City of Harrisburg (PDFs of budgets, legislation, minutes, resolutions, quarterly reports). A council user asks a natural-language question; the system retrieves across three stores and synthesizes a cited answer. The differentiator is cross-table synthesis, not single-document lookup.

`docs/` holds the source PDFs and is treated as ingest input (PDFs are gitignored). Real council data — **do not ingest, extract, or run LLM eval against it without explicit approval; funds are limited.**

## Commands

```bash
./run.sh                      # kills anything on :5001, runs Flask app at localhost:5001
python3 app.py                # same, without the port cleanup

pytest                        # full suite (testpaths=tests, pythonpath=.)
pytest -m "not integration"   # skip tests needing a live DB (CI-safe)
pytest tests/query/test_pipeline.py::test_name   # single test
pytest -m integration         # only the live-DB tests

python scripts/ingest.py              # ingest all PDFs in docs/
python scripts/ingest.py --file X.pdf # single file
python scripts/ingest.py --reingest   # bypass already-ingested check
python scripts/ingest.py --init-only  # create collections/schema only

# Apply SQL schema/migrations manually against $DATABASE_URL (psql).
# sql/schema.sql is the base; sql/migrate_*.sql are dated, apply in order.
```

Dependencies split by purpose: `requirements.txt` (runtime, Vercel), `requirements-ingest.txt` (PDF parsing — pdfplumber, unstructured, tesseract — **local only, not on Vercel**), `requirements-dev.txt` (pytest).

## Architecture

Three-store retrieval-augmented pipeline. There is **no FastAPI/service layer** — [app.py](app.py) (Flask) calls the Python pipeline classes directly. Stores are initialized once at module load in `_init()` so Vercel serverless picks them up; Neo4j failure is non-fatal (graph features degrade gracefully).

**The three stores** ([src/storage/](src/storage/)):
- `VectorStore` — Qdrant, semantic search over all chunks (every chunk is embedded)
- `SQLStore` — PostgreSQL, structured facts (expenditures, metrics, grants, vacancies, resolutions, votes, legislation, goals, meetings…) plus all operational tables (query_logs, chunk_performance, llm_usage, evaluation_*, document_type_registry)
- `GraphStore` — Neo4j, entity relationships (departments, staff, projects)

**Ingestion** ([src/ingestion/pipeline.py](src/ingestion/pipeline.py)): per-document — profile (agentic type/owner/period classification, cheap Haiku) → detect → parse → chunk at section boundaries → tag metadata → classify content type → embed (Voyage) → route to stores. `ingest_directory` runs a bounded `ThreadPoolExecutor` (`ingest_workers`, default 5); one bad document never aborts the batch. Parsing escalates on quality: Unstructured → Tesseract OCR → Vision LLM, gated by readability/garble/text-per-page thresholds in `config.py`.

**Content-type routing is the core ingestion concept.** [src/models.py](src/models.py) defines `CONTENT_TYPES` and `STORAGE_ROUTING` — a dict mapping each content type (`narrative`, `table`, `metrics`, `org_data`, `project`, `header`) to which stores receive it. Everything goes to vector; only some types reach SQL/graph. **Known sharp edge:** structured facts (vacancies, metrics, expenditures) are silently dropped when the classifier lands them in a non-SQL content type — see the memory note on `routes_to_sql`.

**Document types** ([src/ingestion/registry.py](src/ingestion/registry.py)): each registered `DocumentType` (quarterly_report, resolution, minutes, legislation, budget…) carries its own Pydantic `extraction_schema` in [src/ingestion/schemas/](src/ingestion/schemas/) — the LLM extraction contract. New document types are discovered from data via the profiler (low-confidence profiles are quarantined to vector-only) and go through human triage before a schema/DDL is approved. Do not seed types speculatively.

**Query** ([src/query/pipeline.py](src/query/pipeline.py)): `ask()` is exactly 2 synchronous LLM calls — classify (Haiku picks `sources`/`execution`) → retrieve (0 LLM calls, hits the planned stores) → synthesize (Sonnet, cited answer). Weak retrieval triggers a vector fallback. A clarity gate is soft-launched (logged, not enforced). After answering, a sampled (`eval_sample_rate`) background LLM judge scores the response and updates `chunk_performance` — isolated so it can never turn a good answer into a 500.

**Dashboard** ([src/dashboard/](src/dashboard/)): `DashboardAggregator` builds council-facing review questions and next-quarter prompts from SQL facts + goal status; cached 90s. `redesign.html` is the live primary UI served at `/`.

## Conventions & constraints

- **LLM models are config, not hardcoded.** `config.py` `Settings`: Haiku for cheap routing (classifier, profiler), Sonnet for vision + synthesis. Model choices are eval-gated — check the comments before changing them. Costs are tracked in the `llm_usage` table (`/admin/costs`).
- **Embeddings are Voyage** (`voyage-3`, 1024-dim). Config defaults in `config.py` are authoritative; `.env` only overrides them.
- `get_settings()` is `@lru_cache`d — one Settings instance per process.
- Auth is a single `ADMIN_PASSWORD` env check (`/auth`); no user system. Several security/hardening items (read-only `execute_query`, concurrency, cost caps, XSS) are known-deferred by the owner.
- Deployed on Vercel (`vercel.json` routes everything to `app.py`); serverless imposes constraints — heavy ingestion deps are excluded from `requirements.txt` for this reason.
- Tests requiring a live DB must be marked `@pytest.mark.integration`.

## Design docs

`harrisburg_knowledge_base_spec.md` is the original spec. Dated `*_buildnotes.md` / `merge_*.md` files at the root are working design notes. `docs/` also contains many design/planning `.md` files alongside the PDFs.
