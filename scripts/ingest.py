#!/usr/bin/env python3
"""
Ingestion script — ingest all quarterly report PDFs into the knowledge base.

Usage:
  python scripts/ingest.py                          # ingest docs/ directory
  python scripts/ingest.py --docs-dir /path/to/docs # ingest a specific directory
  python scripts/ingest.py --file report.pdf        # ingest a single file
  python scripts/ingest.py --reingest               # skip the already-ingested check
  python scripts/ingest.py --init-only              # only initialize stores, don't ingest
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from src.config import get_settings
from src.ingestion.pipeline import IngestionPipeline

app = typer.Typer(help="Harrisburg Knowledge Base ingestion tool")
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
        format="%(message)s",
    )


@app.command()
def ingest(
    docs_dir: Path = typer.Option(None, "--docs-dir", "-d", help="Directory containing PDF documents"),
    file: Path = typer.Option(None, "--file", "-f", help="Single PDF file to ingest"),
    reingest: bool = typer.Option(False, "--reingest", help="Re-ingest already-processed documents"),
    init_only: bool = typer.Option(False, "--init-only", help="Only initialize stores"),
    log_level: str = typer.Option("INFO", "--log-level"),
):
    """Ingest quarterly report PDFs into the knowledge base."""
    _setup_logging(log_level)
    cfg = get_settings()

    pipeline = IngestionPipeline(cfg)

    console.rule("[bold blue]Harrisburg Knowledge Base — Ingestion")

    # Initialize stores
    console.print("Initializing stores...")
    pipeline.initialize_stores()

    if init_only:
        console.print("[green]Stores initialized. Exiting (--init-only).")
        return

    if file:
        if not file.exists():
            console.print(f"[red]File not found: {file}")
            raise typer.Exit(1)
        console.print(f"Ingesting single file: {file.name}")
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task(f"Ingesting {file.name}...", total=None)
            chunks = pipeline.ingest_document(file)
            progress.update(task, completed=True)
        console.print(f"[green]Done — {len(chunks)} chunks ingested from {file.name}")
        return

    # Directory ingestion
    target_dir = docs_dir or Path(cfg.docs_dir)
    if not target_dir.exists():
        console.print(f"[red]Docs directory not found: {target_dir}")
        raise typer.Exit(1)

    pdfs = sorted(target_dir.glob("*.pdf"))
    if not pdfs:
        console.print(f"[yellow]No PDF files found in {target_dir}")
        return

    console.print(f"Found [bold]{len(pdfs)}[/bold] PDF documents in {target_dir}")

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(), console=console,
    ) as progress:
        overall = progress.add_task("Ingesting documents...", total=len(pdfs))
        success, failed = 0, 0

        for pdf in pdfs:
            if not reingest and pipeline.sql_store.is_document_ingested(pdf.name):
                console.print(f"  [dim]Skipping (already ingested): {pdf.name}")
                progress.advance(overall)
                continue

            progress.update(overall, description=f"Ingesting {pdf.name}...")
            try:
                chunks = pipeline.ingest_document(pdf)
                console.print(f"  [green]✓ {pdf.name} ({len(chunks)} chunks)")
                success += 1
            except Exception as e:
                console.print(f"  [red]✗ {pdf.name}: {e}")
                failed += 1
            progress.advance(overall)

    console.rule()
    console.print(f"[bold]Ingestion complete: {success} succeeded, {failed} failed")

    # Seed evaluation suite if it's empty
    try:
        from src.evaluation.suite import EvaluationSuite
        suite = EvaluationSuite(pipeline.sql_store, cfg)
        suite.seed_questions()
        console.print("[green]Evaluation suite seeded")
    except Exception as e:
        console.print(f"[yellow]Could not seed evaluation suite: {e}")


if __name__ == "__main__":
    app()
