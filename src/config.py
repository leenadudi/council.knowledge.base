"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM providers
    anthropic_api_key: str = ""

    # Embeddings (Voyage AI — free tier, no OpenAI needed)
    voyage_api_key: str = ""
    embedding_model: str = "voyage-3"
    embedding_provider: str = "voyage"
    embedding_dimensions: int = 1024  # voyage-3 default

    # Vision LLM (complex PDF parsing)
    vision_model: str = "claude-sonnet-4-6"
    vision_provider: str = "anthropic"

    # Synthesis / query LLM
    synthesis_model: str = "claude-sonnet-4-6"
    synthesis_provider: str = "anthropic"

    # Query classifier model — split from synthesis so the cheap routing task
    # uses a smaller model. Phase 3 eval gate (2026-06-25) confirmed Haiku 4.5
    # holds answer accuracy (5.00→5.00, pass-rate 12/12) at ~3x lower cost.
    query_classifier_model: str = "claude-haiku-4-5"

    # Per-query LLM judge runs on this fraction of queries (1.0 = every query).
    # Background quality monitoring only — sampling has no user-facing effect.
    eval_sample_rate: float = 0.1

    # PostgreSQL
    database_url: str = "postgresql://postgres:password@localhost:5432/harrisburg_kb"

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # LangSmith observability
    langsmith_api_key: str = ""
    langsmith_project: str = "harrisburg-kb"
    langchain_tracing_v2: str = "false"

    # Agentic profiler (document type/owner/period classification)
    profiler_model: str = "claude-haiku-4-5"   # cheap routing task, eval-validated
    profiler_max_pages: int = 3                 # only the first N pages are read to classify
    profile_confidence_threshold: float = 0.55  # below this → quarantine (vector-only)

    # Parallel ingestion (bounded worker pool over independent documents)
    ingest_workers: int = 5                     # cap to stay under API/DB rate limits

    # Ingestion
    docs_dir: str = "/tmp/docs"
    max_chunk_size: int = 1500
    min_chunk_size: int = 100
    chunk_overlap: int = 100
    extraction_batch_size: int = 8

    # Garbled text detection threshold for fallback to vision LLM
    garbled_ratio_threshold: float = 0.10
    text_per_page_threshold: int = 100   # chars/page below which vision LLM kicks in

    # Readability gate: docs whose parsed text scores below this are treated as
    # garbled (bad OCR) and re-read with the Vision LLM. See src/ingestion/quality.py.
    garble_readability_threshold: float = 0.35
    enable_vision_escalation: bool = True

    # Tesseract OCR (scanned/low-text PDFs)
    ocr_dpi: int = 200                    # render resolution for OCR
    ocr_min_chars_per_page: int = 150     # below this, OCR is poor -> fall back to Vision LLM

    # Query clarity gate (soft-launch: logged only, not enforced)
    # Conservative by design: only flag genuinely unanswerable queries. Most
    # questions answer well, so a false "did you mean…?" is worse than answering.
    clarity_gate_enabled: bool = False         # when True, weak queries return suggestions instead of an answer
    clarity_min_top_score: float = 0.30        # best chunk below this → weak (nothing semantically close). Sole trigger.
    clarity_min_mean_score: float = 0.25       # recorded for calibration; does not trigger on its own
    clarity_max_header_ratio: float = 0.80     # recorded for calibration; does not trigger on its own

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
