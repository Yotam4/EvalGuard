"""``evalguard view`` — show recent runs from the local SQLite store."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from evalguard_cli.local.sqlite_store import SqliteStore

console = Console()


def view(
    db_path: Path = typer.Option(Path(".evalguard/local.db"), "--db", help="SQLite path"),
    limit: int = typer.Option(10, "--limit", "-n", help="How many recent runs to show"),
    last: bool = typer.Option(False, "--last", help="Show details of the most recent run"),
) -> None:
    """List recent runs (or details of the latest run)."""
    if not db_path.exists():
        console.print(f"[yellow]no run history at {db_path}[/yellow]")
        raise typer.Exit(1)
    store = SqliteStore(db_path)
    runs = store.list_runs(limit=limit)
    if not runs:
        console.print("[yellow]no runs yet[/yellow]")
        return

    if last:
        last_run = runs[0]
        metrics = store.compute_metrics(last_run["run_id"])
        console.print(f"[bold]Run {last_run['run_id']}[/bold]  status={last_run['status']}")
        for k, v in metrics.items():
            console.print(f"  {k}: {v}")
        return

    table = Table(title="EvalGuard runs")
    table.add_column("run_id", style="cyan", no_wrap=True)
    table.add_column("started", style="dim")
    table.add_column("status")
    table.add_column("rows", justify="right")
    table.add_column("cost_usd", justify="right")
    for r in runs:
        status_style = "green" if r["status"] == "passed" else ("red" if r["status"] == "failed" else "yellow")
        table.add_row(
            r["run_id"],
            r["started_at"],
            f"[{status_style}]{r['status']}[/{status_style}]",
            str(r["row_count"]),
            f"${r['cost_usd']:.4f}",
        )
    console.print(table)
