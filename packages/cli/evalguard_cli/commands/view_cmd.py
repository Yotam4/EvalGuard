"""``evalguard view`` — show recent runs from the local SQLite store."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from evalguard_cli.console import console
from evalguard_cli.local.gate import GateResult, format_gate_report
from evalguard_cli.local.sqlite_store import SqliteStore


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
        gate_results = store.get_gate_results(last_run["run_id"])
        console.print(
            f"[bold]Run {last_run['run_id']}[/bold]  "
            f"status={_color_status(last_run.get('status'))}  "
            f"row_status={last_run.get('row_status')}  "
            f"gate_status={last_run.get('gate_status')}"
        )
        for k, v in metrics.items():
            if k in {"by_evaluator", "by_layer", "by_tag"}:
                continue
            console.print(f"  {k}: {v}")
        if gate_results:
            console.print()
            console.print(format_gate_report([
                GateResult(
                    name=g["gate_name"],
                    blocking=g["blocking"],
                    passed=g["passed"],
                    details=g["details"],
                    severity=g.get("severity") or ("block" if g["blocking"] else "warn"),
                    layer=g.get("layer"),
                )
                for g in gate_results
            ]))
        return

    table = Table(title="EvalGuard runs")
    table.add_column("run_id", style="cyan", no_wrap=True)
    table.add_column("started", style="dim")
    table.add_column("status")
    table.add_column("rows", justify="right")
    table.add_column("cost_usd", justify="right")
    for r in runs:
        table.add_row(
            r["run_id"],
            r["started_at"],
            _color_status(r.get("status")),
            str(r.get("row_count") or 0),
            f"${(r.get('cost_usd') or 0.0):.4f}",
        )
    console.print(table)


def _color_status(status: str | None) -> str:
    color = {
        "passed":      "green",
        "warned":      "yellow",
        "row_failed":  "red",
        "gate_failed": "red",
        "cost_capped": "yellow",
        None:          "dim",
        "running":     "dim",
    }.get(status, "white")
    return f"[{color}]{status or '-'}[/{color}]"
