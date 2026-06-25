#!/usr/bin/env python3
"""
Evaluation script — run the evaluation suite and print a quality report.

Usage:
  python scripts/evaluate.py                    # run all 50 questions
  python scripts/evaluate.py --store sql        # only SQL category questions
  python scripts/evaluate.py --report-only      # print last run results without re-running
  python scripts/evaluate.py --quarterly-review # run full quarterly review workflow
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from src.config import get_settings
from src.evaluation.evaluator import Evaluator
from src.evaluation.suite import EvaluationSuite
from src.query.pipeline import QueryPipeline
from src.storage.graph_store import GraphStore
from src.storage.sql_store import SQLStore
from src.storage.vector_store import VectorStore

app = typer.Typer(help="Harrisburg Knowledge Base evaluation tool")
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
        format="%(message)s",
    )


@app.command()
def run(
    store: str = typer.Option(None, "--store", help="Filter by store type: sql|vector|graph|cross"),
    report_only: bool = typer.Option(False, "--report-only", help="Show last run only"),
    quarterly_review: bool = typer.Option(False, "--quarterly-review", help="Run full quarterly improvement cycle"),
    log_level: str = typer.Option("WARNING", "--log-level"),
):
    """Run the evaluation suite and display a quality report."""
    _setup_logging(log_level)
    cfg = get_settings()

    sql_store = SQLStore(cfg)
    sql_store.connect()

    console.rule("[bold blue]Harrisburg Knowledge Base — Evaluation")

    if report_only:
        _print_last_run(sql_store)
        return

    if quarterly_review:
        _run_quarterly_review(sql_store, cfg)
        return

    # Set up query pipeline
    vector_store = VectorStore(cfg)
    graph_store = GraphStore(cfg)
    graph_store.connect()
    pipeline = QueryPipeline(vector_store, sql_store, graph_store, cfg)
    evaluator = Evaluator(cfg)
    suite = EvaluationSuite(sql_store, cfg)

    # Optionally filter questions
    questions = sql_store.get_evaluation_suite()
    if store:
        questions = [q for q in questions if q.get("store_type") == store]
        console.print(f"Running [bold]{len(questions)}[/bold] questions (store_type={store})")
    else:
        console.print(f"Running [bold]{len(questions)}[/bold] evaluation questions")

    results = suite.run(pipeline, evaluator)
    report = suite.report(results)
    _print_report(report)

    graph_store.close()
    sql_store.close()


def _print_report(report: dict) -> None:
    if not report:
        console.print("[yellow]No results to display")
        return

    console.print()
    table = Table(title="Evaluation Results", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")

    table.add_row("Total questions", str(report["total_questions"]))
    table.add_row("Passed (avg ≥ 3.0)", f"[green]{report['passed']}[/green]")
    table.add_row("Failed", f"[red]{report['failed']}[/red]")
    table.add_row("Pass rate", f"{report['pass_rate']:.0%}")
    table.add_row("Avg retrieval score", f"{report['avg_retrieval_score']:.2f}/5")
    table.add_row("Avg accuracy score", f"{report['avg_accuracy_score']:.2f}/5")
    table.add_row("Avg completeness score", f"{report['avg_completeness_score']:.2f}/5")

    console.print(table)

    if report.get("failure_summary"):
        console.print("\n[bold red]Top failures:[/bold red]")
        for failure in report["failure_summary"]:
            console.print(f"  • {failure['question']}")
            console.print(f"    Scores: {failure['scores']}", style="dim")


def _print_last_run(sql_store: SQLStore) -> None:
    rows = sql_store.execute_query(
        """SELECT run_id, run_date,
                  COUNT(*) as total,
                  SUM(CASE WHEN passed THEN 1 ELSE 0 END) as passed,
                  AVG(retrieval_score) as avg_retrieval,
                  AVG(accuracy_score) as avg_accuracy,
                  AVG(completeness_score) as avg_completeness
           FROM evaluation_results
           GROUP BY run_id, run_date
           ORDER BY run_date DESC
           LIMIT 1"""
    )
    if not rows:
        console.print("[yellow]No evaluation runs found")
        return

    row = rows[0]
    report = {
        "total_questions": row["total"],
        "passed": row["passed"],
        "failed": row["total"] - row["passed"],
        "pass_rate": row["passed"] / row["total"],
        "avg_retrieval_score": float(row["avg_retrieval"] or 0),
        "avg_accuracy_score": float(row["avg_accuracy"] or 0),
        "avg_completeness_score": float(row["avg_completeness"] or 0),
    }
    console.print(f"Run ID: {row['run_id']} ({row['run_date']})")
    _print_report(report)


def _run_quarterly_review(sql_store: SQLStore, cfg) -> None:
    """
    Step 1 of the quarterly improvement cycle:
    Pull low-scoring queries, identify patterns, generate a failure analysis report.
    """
    console.rule("[bold yellow]Quarterly Review")

    low_scoring = sql_store.get_low_scoring_queries(min_score=3.0)
    console.print(f"Found [bold]{len(low_scoring)}[/bold] queries with score < 3.0 this quarter")

    if not low_scoring:
        console.print("[green]No low-scoring queries — system quality is good!")
        return

    # Group by store type and question pattern
    by_store: dict[str, int] = {}
    for q in low_scoring:
        classification = q.get("classification") or {}
        stores = classification.get("sources", ["unknown"])
        for store in stores:
            by_store[store] = by_store.get(store, 0) + 1

    console.print("\n[bold]Failures by store:[/bold]")
    for store, count in sorted(by_store.items(), key=lambda x: -x[1]):
        console.print(f"  {store}: {count} failures")

    console.print("\n[bold]Top failing questions:[/bold]")
    for q in low_scoring[:10]:
        console.print(f"  • {q['question'][:80]}")

    console.print(
        "\n[dim]Next steps: fix chunking issues, update prompts for failure patterns, "
        "then run: python scripts/ingest.py --reingest && python scripts/evaluate.py[/dim]"
    )


if __name__ == "__main__":
    app()
