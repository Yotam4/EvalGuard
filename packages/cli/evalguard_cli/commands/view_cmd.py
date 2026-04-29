"""``evalguard view`` — drill into recent runs from the local SQLite store.

Three modes:

    evalguard view                    # list recent runs
    evalguard view <run_id>           # show summary + per-row table for that run
    evalguard view <run_id> --row R   # full detail for one row (scores + raw judge)
    evalguard view --last             # shorthand for the most recent run
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from evalguard_cli.console import console
from evalguard_cli.local.gate import GateResult, format_gate_report
from evalguard_cli.local.sqlite_store import SqliteStore


def view(
    run_id: str | None = typer.Argument(None, help="Run ID to inspect (omit to list recent runs)"),
    row: str | None = typer.Option(None, "--row", "-r", help="Drill into a specific row of the run"),
    layer: int | None = typer.Option(None, "--layer", "-l", help="Filter row scores by layer (1..5)"),
    last: bool = typer.Option(False, "--last", help="Use the most recent run regardless of run_id"),
    db_path: Path = typer.Option(Path(".evalguard/local.db"), "--db", help="SQLite path"),
    limit: int = typer.Option(10, "--limit", "-n", help="When listing, how many runs to show"),
) -> None:
    """List runs, summarize a run, or drill into a row."""
    if not db_path.exists():
        console.print(f"[yellow]no run history at {db_path}[/yellow]")
        raise typer.Exit(1)
    store = SqliteStore(db_path)

    runs = store.list_runs(limit=max(limit, 1))
    if not runs:
        console.print("[yellow]no runs yet[/yellow]")
        return

    if last:
        run_id = runs[0]["run_id"]

    if run_id is None and row is None:
        _render_runs_list(runs)
        return

    if run_id is None:
        console.print("[red]--row requires a run_id (or pass --last)[/red]")
        raise typer.Exit(1)

    if row is not None:
        _render_row_detail(store, run_id, row, layer=layer)
        return

    _render_run_summary(store, run_id, runs)


# ---------------------------------------------------------------------------


def _render_runs_list(runs: list[dict]) -> None:
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


def _render_run_summary(store: SqliteStore, run_id: str, runs: list[dict]) -> None:
    matching = [r for r in runs if r["run_id"].startswith(run_id)]
    if not matching:
        all_runs = store.list_runs(limit=1000)
        matching = [r for r in all_runs if r["run_id"].startswith(run_id)]
    if not matching:
        console.print(f"[red]no run found matching {run_id}[/red]")
        raise typer.Exit(1)
    full = matching[0]
    metrics = store.compute_metrics(full["run_id"])
    gate_results = store.get_gate_results(full["run_id"])

    console.print(
        f"[bold]Run {full['run_id']}[/bold]  "
        f"status={_color_status(full.get('status'))}  "
        f"rows={full.get('row_pass_count', 0)}/{full.get('row_count', 0)} pass  "
        f"cost=${(full.get('cost_usd') or 0):.4f}"
    )
    console.print(f"config_hash={full.get('config_hash', '')[:16]}…")

    # Per-layer roll-up panel
    if metrics.get("by_layer"):
        layer_table = Table(title="Per-layer metrics", show_lines=False)
        layer_table.add_column("layer", style="cyan")
        layer_table.add_column("evaluators", style="dim")
        layer_table.add_column("rows", justify="right")
        layer_table.add_column("pass_rate", justify="right")
        layer_table.add_column("row_pass_rate", justify="right")
        layer_table.add_column("mean", justify="right")
        for layer_idx in sorted(metrics["by_layer"]):
            agg = metrics["by_layer"][layer_idx]
            layer_table.add_row(
                f"L{layer_idx}",
                ", ".join(agg.get("evaluators", [])),
                str(agg.get("rows_evaluated", 0)),
                f"{agg.get('pass_rate', 0.0):.3f}",
                f"{agg.get('row_pass_rate', 0.0):.3f}",
                f"{agg.get('mean', 0.0):.3f}",
            )
        console.print(layer_table)

    if gate_results:
        console.print(format_gate_report([
            GateResult(
                name=g["gate_name"], blocking=g["blocking"], passed=g["passed"],
                details=g["details"],
                severity=g.get("severity") or ("block" if g["blocking"] else "warn"),
                layer=g.get("layer"),
            )
            for g in gate_results
        ]))

    rows = store.list_rows(full["run_id"])
    rows_table = Table(title=f"Rows ({len(rows)})")
    rows_table.add_column("row_id", style="cyan")
    rows_table.add_column("status", justify="center")
    rows_table.add_column("scores", justify="right")
    rows_table.add_column("model", style="dim")
    rows_table.add_column("cost", justify="right")
    rows_table.add_column("tags", style="dim")
    for r in rows:
        marker = "[green]✓[/green]" if r["passed"] else "[red]✗[/red]"
        rows_table.add_row(
            r["row_id"], marker, str(r["n_scores"]),
            r["model"] or "-", f"${r['cost_usd']:.4f}",
            ", ".join(r["tags"]) if r["tags"] else "",
        )
    console.print(rows_table)
    console.print(
        "[dim]Drill into a row with[/dim] "
        f"[cyan]evalguard view {full['run_id'][:16]} --row <row_id>[/cyan]"
    )


def _render_row_detail(store: SqliteStore, run_id: str, row_id: str, *, layer: int | None) -> None:
    matching = store.list_runs(limit=1000)
    full = next((r for r in matching if r["run_id"].startswith(run_id)), None)
    if not full:
        console.print(f"[red]no run found matching {run_id}[/red]")
        raise typer.Exit(1)
    detail = store.get_row(full["run_id"], row_id)
    if detail is None:
        console.print(f"[red]row {row_id} not found in {full['run_id']}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[bold]Row {row_id}[/bold] of [cyan]{full['run_id']}[/cyan]  "
        f"model={detail['model']}  cost=${detail['cost_usd']:.4f}  "
        f"latency={detail['latency_ms']}ms  cache_hit={detail['cache_hit']}"
    )
    console.print(Panel(_pretty(detail["input"]),    title="input",    border_style="blue"))
    if detail["expected"] is not None:
        console.print(Panel(_pretty(detail["expected"]), title="expected", border_style="dim"))
    console.print(Panel(detail["output"] or "", title="output", border_style="green"))

    scores = detail["scores"]
    if layer is not None:
        scores = [s for s in scores if s["layer"] == layer]

    score_table = Table(title="Scores")
    score_table.add_column("layer", justify="center")
    score_table.add_column("evaluator", style="cyan")
    score_table.add_column("kind", style="dim")
    score_table.add_column("value", justify="right")
    score_table.add_column("passed", justify="center")
    for s in scores:
        marker = "[green]✓[/green]" if s["passed"] else "[red]✗[/red]"
        score_table.add_row(
            f"L{s['layer']}", s["evaluator_id"], s["evaluator_kind"],
            f"{s['value']:.3f}", marker,
        )
    console.print(score_table)

    # Raw judge / heuristic payloads (most useful for L3 judges).
    for s in scores:
        if not s["raw"]:
            continue
        label = f"L{s['layer']} · {s['evaluator_id']} · raw"
        console.print(Panel(_pretty(s["raw"]), title=label, border_style="dim"))


def _pretty(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


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
