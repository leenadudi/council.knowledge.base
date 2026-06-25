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

    # Ingestion
    docs_dir: str = "/tmp/docs"
    max_chunk_size: int = 1500
    min_chunk_size: int = 100
    chunk_overlap: int = 100
    extraction_batch_size: int = 8

    # Garbled text detection threshold for fallback to vision LLM
    garbled_ratio_threshold: float = 0.10
    text_per_page_threshold: int = 100   # chars/page below which vision LLM kicks in

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
