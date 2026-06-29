#!/usr/bin/env python3
"""Re-ingest all documents through the agentic pipeline (content-derived metadata).

Usage:
  python scripts/reingest.py              # re-ingest cfg.docs_dir (skip_existing=False)
  python scripts/reingest.py docs/        # re-ingest a specific directory
"""

import logging
import sys
from pathlib import Path

# Ensure src/ is on the path when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings
from src.ingestion.pipeline import IngestionPipeline

logging.basicConfig(level=logging.INFO)


def main(docs_dir=None):
    cfg = get_settings()
    pipe = IngestionPipeline(cfg)
    pipe.initialize_stores()
    pipe.ingest_directory(docs_dir or cfg.docs_dir, skip_existing=False)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
