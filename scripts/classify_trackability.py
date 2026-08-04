#!/usr/bin/env python3
"""
Goal trackability classification — AI-suggest + human confirm.

Spends LLM tokens ONLY with an explicit --yes. The intended order of use:

  python scripts/classify_trackability.py export-sample          # no LLM: 40-goal CSV for hand labeling
  python scripts/classify_trackability.py pilot labeled.csv --yes  # classify the labeled sample, report agreement
  python scripts/classify_trackability.py run --dry-run          # no LLM: count + cost estimate
  python scripts/classify_trackability.py run --yes              # full classification of unlabeled goals

Human labels (trackability_source='human') are never overwritten.
Design: docs/superpowers/specs/2026-07-29-goal-trackability-design.md
"""
from __future__ import annotations

import csv
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import typer
from rich.console import Console
from rich.table import Table

from src.config import get_settings
from src.extraction.trackability import (
    LABELS, NEEDS_REVIEW, LogicalGoal, classify_goal, collapse_goals,
    apply_result, goal_group_key,
)
from src.storage.sql_store import SQLStore

app = typer.Typer(help="Classify goal trackability (measurable/milestone/aspiration)")
console = Console()

_GOAL_COLS = ("id, department, year, quarter, goal_title, description, target, "
              "status, trackability, trackability_source")

# ~900 prompt tokens in + ~120 out per goal on the configured Haiku-class model.
_EST_COST_PER_GOAL = 0.0015


def _load_logical_goals(store: SQLStore) -> list[LogicalGoal]:
    with store.cursor() as cur:
        cur.execute(f"SELECT {_GOAL_COLS} FROM goals")
        rows = [dict(r) for r in cur.fetchall()]
    return collapse_goals(rows)


def _confirm_spend(n: int, yes: bool) -> None:
    est = n * _EST_COST_PER_GOAL
    console.print(f"[bold]{n}[/bold] goals to classify — estimated cost ≈ ${est:.2f} "
                  f"(model: {get_settings().trackability_model})")
    if not yes:
        console.print("[red]Refusing to spend without --yes.[/red] "
                      "Re-run with --yes to proceed, or use --dry-run / export-sample.")
        raise typer.Exit(1)


@app.command("export-sample")
def export_sample(
    out: Path = typer.Option(Path("trackability_sample.csv"), help="CSV to write"),
    n: int = typer.Option(40, help="Sample size"),
    seed: int = typer.Option(20260803, help="Shuffle seed (stable sample)"),
):
    """Export a stratified sample of logical goals for hand labeling. No LLM."""
    goals = _load_logical_goals(SQLStore(get_settings()))
    # Stratify across departments: round-robin so no single big department dominates.
    by_dept: dict[str, list[LogicalGoal]] = {}
    for g in goals:
        by_dept.setdefault(goal_group_key(g.department, g.title)[0], []).append(g)
    rng = random.Random(seed)
    for bucket in by_dept.values():
        rng.shuffle(bucket)
    sample: list[LogicalGoal] = []
    while len(sample) < min(n, len(goals)):
        for bucket in by_dept.values():
            if bucket and len(sample) < n:
                sample.append(bucket.pop())
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["row_ids", "department", "title", "target", "latest_note",
                    "human_label (measurable|milestone|aspiration)"])
        for g in sample:
            w.writerow([";".join(map(str, g.row_ids)), g.department, g.title,
                        g.target, (g.status_notes[-1] if g.status_notes else ""), ""])
    console.print(f"Wrote {len(sample)} goals to [bold]{out}[/bold]. "
                  "Fill the last column, then run: pilot <csv> --yes")


@app.command("pilot")
def pilot(
    labeled_csv: Path = typer.Argument(..., help="export-sample CSV with human_label filled"),
    yes: bool = typer.Option(False, "--yes", help="Confirm LLM spend"),
    write: bool = typer.Option(False, help="Also persist labels for agreeing rows"),
):
    """Classify the hand-labeled sample and report agreement. Spends LLM tokens."""
    with labeled_csv.open() as f:
        rows = [r for r in csv.DictReader(f)]
    label_col = next(c for c in rows[0] if c.startswith("human_label"))
    labeled = [r for r in rows if (r.get(label_col) or "").strip().lower() in LABELS]
    if not labeled:
        console.print("[red]No rows with a valid human_label — fill the CSV first.[/red]")
        raise typer.Exit(1)
    _confirm_spend(len(labeled), yes)

    cfg = get_settings()
    store = SQLStore(cfg)
    by_ids = {";".join(map(str, g.row_ids)): g for g in _load_logical_goals(store)}
    agree = 0
    table = Table("title", "human", "ai", "conf", "match")
    for r in labeled:
        goal = by_ids.get(r["row_ids"])
        if goal is None:
            console.print(f"[yellow]skip (not in DB): {r['title'][:50]}[/yellow]")
            continue
        result = classify_goal(goal, settings=cfg)
        human = r[label_col].strip().lower()
        match = result.label == human
        agree += match
        table.add_row(goal.title[:48], human, result.label,
                      f"{result.confidence:.2f}", "✓" if match else "✗")
        if write and match:
            with store.cursor() as cur:
                apply_result(cur, goal, result, cfg.trackability_confidence_threshold)
    console.print(table)
    console.print(f"[bold]Agreement: {agree}/{len(labeled)} "
                  f"({100 * agree / len(labeled):.0f}%)[/bold] — design gate is ≥90%.")


@app.command("run")
def run(
    yes: bool = typer.Option(False, "--yes", help="Confirm LLM spend"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count + cost only, no LLM"),
    limit: int = typer.Option(0, help="Classify at most N goals (0 = all)"),
):
    """Classify every logical goal that has no label yet. Spends LLM tokens."""
    cfg = get_settings()
    store = SQLStore(cfg)
    todo = [g for g in _load_logical_goals(store)
            if not g.has_human_label and not g.trackability]
    if limit:
        todo = todo[:limit]
    if dry_run:
        est = len(todo) * _EST_COST_PER_GOAL
        console.print(f"{len(todo)} unlabeled logical goals; estimated cost ≈ ${est:.2f}. "
                      "No LLM calls made.")
        return
    _confirm_spend(len(todo), yes)
    counts: dict[str, int] = {}
    for i, goal in enumerate(todo, 1):
        try:
            result = classify_goal(goal, settings=cfg)
        except Exception as e:
            console.print(f"[red]failed[/red] {goal.title[:60]}: {e}")
            continue
        with store.cursor() as cur:
            stored = apply_result(cur, goal, result, cfg.trackability_confidence_threshold)
        counts[stored] = counts.get(stored, 0) + 1
        console.print(f"[{i}/{len(todo)}] {stored:<12} {result.confidence:.2f}  {goal.title[:60]}")
    console.print(f"\nDone: {counts}")
    if counts.get(NEEDS_REVIEW):
        console.print(f"{counts[NEEDS_REVIEW]} goals quarantined to needs_review — "
                      "triage them in the Goals tab.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    app()
