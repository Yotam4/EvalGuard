"""``evalguard view`` — drill into recent runs from the local SQLite store.

Modes:

    evalguard view                     # list recent runs
    evalguard view <run_id>            # summary + per-trial table + gates
    evalguard view <run_id> --trial T  # one trial: rows + gates + per-layer
    evalguard view <run_id> --row R    # one row: scores + raw judge output
    evalguard view <run_id> --json     # stable JSON contract (for piping)
    evalguard view --last [...]        # shorthand for the most recent run
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from evalguard_cli.console import console
from evalguard_cli.local.gate import GateResult, format_gate_report
from evalguard_cli.local.serializer import run_to_dict, run_to_json
from evalguard_cli.local.sqlite_store import SqliteStore


def view(
    run_id: str | None = typer.Argument(None, help="Run ID to inspect (omit to list runs)"),
    row: str | None = typer.Option(None, "--row", "-r", help="Drill into a specific row of the run"),
    trial: str | None = typer.Option(None, "--trial", "-t", help="Scope to a specific trial of the run"),
    layer: int | None = typer.Option(None, "--layer", "-l", help="Filter scores by pyramid layer (1..5)"),
    last: bool = typer.Option(False, "--last", help="Use the most recent run regardless of run_id"),
    as_json: bool = typer.Option(False, "--json", help="Emit the stable JSON contract to stdout"),
    include_scores: bool = typer.Option(False, "--scores", help="Include full per-row scores in --json"),
    db_path: Path = typer.Option(Path(".evalguard/local.db"), "--db", help="SQLite path"),
    limit: int = typer.Option(10, "--limit", "-n", help="When listing, how many runs to show"),
) -> None:
    """List runs, summarize a run, or drill into a trial / row."""
    if not db_path.exists():
        console.print(f"[yellow]no run history at {db_path}[/yellow]")
        raise typer.Exit(1)
    store = SqliteStore(db_path)

    runs = store.list_runs(limit=max(limit, 1000 if last or as_json else limit))
    if not runs:
        console.print("[yellow]no runs yet[/yellow]")
        return

    if last:
        run_id = runs[0]["run_id"]

    if run_id is None and row is None and trial is None and not as_json:
        _render_runs_list(runs[:limit])
        return

    full = _resolve_run(runs, run_id) if run_id else None
    if run_id and full is None:
        console.print(f"[red]no run found matching {run_id}[/red]")
        raise typer.Exit(1)

    # JSON output is the contract — everything else is a rich-rendered view.
    if as_json:
        if full is None:
            console.print("[red]--json requires a run_id (or pass --last)[/red]")
            raise typer.Exit(1)
        print(run_to_json(store, full["run_id"], include_scores=include_scores))
        return

    if (row is not None or trial is not None) and full is None:
        console.print("[red]--row/--trial require a run_id (or pass --last)[/red]")
        raise typer.Exit(1)

    if row is not None:
        _render_row_detail(store, full["run_id"], row, layer=layer)
        return

    if trial is not None:
        _render_trial_detail(store, full, trial)
        return

    _render_run_summary(store, full)


# ---------------------------------------------------------------------------


def _resolve_run(runs: list[dict], run_id: str) -> dict | None:
    matching = [r for r in runs if r["run_id"].startswith(run_id)]
    return matching[0] if matching else None


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


def _render_run_summary(store: SqliteStore, full: dict) -> None:
    run_id = full["run_id"]
    trials = store.list_trials(run_id)

    console.print(
        f"[bold]Run {run_id}[/bold]  "
        f"status={_color_status(full.get('status'))}  "
        f"rows={full.get('row_pass_count', 0)}/{full.get('row_count', 0)} pass  "
        f"cost=${(full.get('cost_usd') or 0):.4f}  "
        f"trials={len(trials)}"
    )
    console.print(f"config_hash={full.get('config_hash', '')[:16]}…")

    # Per-trial table
    if trials:
        ttable = Table(title=f"Trials ({len(trials)})")
        ttable.add_column("trial_id", style="cyan", no_wrap=True)
        ttable.add_column("provider_id", style="dim")
        ttable.add_column("status", justify="center")
        ttable.add_column("gate_status", justify="center")
        ttable.add_column("rows", justify="right")
        ttable.add_column("cost", justify="right")
        for t in trials:
            ttable.add_row(
                t["trial_id"],
                t["provider_id"],
                _color_status(t["status"]),
                _color_status(t["gate_status"]),
                f"{t['row_pass_count']}/{t['row_count']}",
                f"${t['cost_usd']:.4f}",
            )
        console.print(ttable)

    # Cross-trial winners
    if len(trials) >= 2:
        comparison = store.compute_comparison(run_id)
        if comparison.get("best_by"):
            cmp_table = Table(title="Best by metric")
            cmp_table.add_column("metric", style="cyan")
            cmp_table.add_column("winner", style="green")
            cmp_table.add_column("value", justify="right")
            cmp_table.add_column("runner-up", style="dim")
            cmp_table.add_column("Δ", justify="right")
            for key, row in sorted(comparison["best_by"].items()):
                if key.endswith(".pass_rate") or key.startswith("layer") and key.endswith(".row_pass_rate"):
                    continue   # noisier metrics live in --json
                delta = row["winner"]["value"] - row["runner_up"]["value"]
                cmp_table.add_row(
                    key,
                    row["winner"]["provider_id"],
                    f"{row['winner']['value']:.4f}",
                    row["runner_up"]["provider_id"],
                    f"{delta:+.4f}",
                )
            console.print(cmp_table)

    # Per-trial gates (concatenated, since each is independently evaluated)
    for t in trials:
        gates = store.get_gate_results(run_id, trial_id=t["trial_id"])
        if not gates:
            continue
        console.print(f"\n[bold]Gates · {t['provider_id']}[/bold]")
        console.print(format_gate_report([
            GateResult(
                name=g["gate_name"], blocking=g["blocking"], passed=g["passed"],
                details=g["details"],
                severity=g.get("severity") or ("block" if g["blocking"] else "warn"),
                layer=g.get("layer"),
            )
            for g in gates
        ]))

    console.print(
        f"\n[dim]Drill into a trial:[/dim] "
        f"[cyan]evalguard view {run_id[:16]} --trial {trials[0]['trial_id'][:16] if trials else 'TRIAL'}[/cyan]"
    )
    console.print(
        f"[dim]Drill into a row:[/dim]    "
        f"[cyan]evalguard view {run_id[:16]} --row <row_id>[/cyan]"
    )
    console.print(
        f"[dim]Stable JSON:[/dim]         "
        f"[cyan]evalguard view {run_id[:16]} --json [--scores][/cyan]"
    )


def _render_trial_detail(store: SqliteStore, full: dict, trial_arg: str) -> None:
    trials = store.list_trials(full["run_id"])
    matching = [t for t in trials if t["trial_id"].startswith(trial_arg)]
    if not matching:
        console.print(f"[red]no trial found matching {trial_arg}[/red]")
        raise typer.Exit(1)
    t = matching[0]

    console.print(
        f"[bold]Trial {t['trial_id']}[/bold] of [cyan]{full['run_id']}[/cyan]\n"
        f"provider={t['provider_id']}  model={t['model']}  "
        f"status={_color_status(t['status'])}  "
        f"gate_status={_color_status(t['gate_status'])}  "
        f"cost=${t['cost_usd']:.4f}"
    )
    if t["config"]:
        console.print(Panel(json.dumps(t["config"], indent=2),
                            title="provider config", border_style="dim"))

    metrics = store.compute_metrics(full["run_id"], trial_id=t["trial_id"])
    if metrics.get("by_layer"):
        layer_table = Table(title="Per-layer metrics")
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

    gates = store.get_gate_results(full["run_id"], trial_id=t["trial_id"])
    if gates:
        console.print(format_gate_report([
            GateResult(
                name=g["gate_name"], blocking=g["blocking"], passed=g["passed"],
                details=g["details"],
                severity=g.get("severity") or ("block" if g["blocking"] else "warn"),
                layer=g.get("layer"),
            )
            for g in gates
        ]))

    rows = store.list_rows(full["run_id"], trial_id=t["trial_id"])
    rows_table = Table(title=f"Rows ({len(rows)})")
    rows_table.add_column("row_id", style="cyan")
    rows_table.add_column("status", justify="center")
    rows_table.add_column("scores", justify="right")
    rows_table.add_column("cost", justify="right")
    rows_table.add_column("tags", style="dim")
    for r in rows:
        marker = "[green]✓[/green]" if r["passed"] else "[red]✗[/red]"
        rows_table.add_row(
            r["row_id"], marker, str(r["n_scores"]),
            f"${r['cost_usd']:.4f}",
            ", ".join(r["tags"]) if r["tags"] else "",
        )
    console.print(rows_table)


def _render_row_detail(store: SqliteStore, run_id: str, row_id: str, *, layer: int | None) -> None:
    detail = store.get_row(run_id, row_id)
    if detail is None:
        console.print(f"[red]row {row_id} not found in {run_id}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[bold]Row {row_id}[/bold] of [cyan]{run_id}[/cyan]  "
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
        "failed":      "red",
        None:          "dim",
        "running":     "dim",
        "none":        "dim",
    }.get(status, "white")
    return f"[{color}]{status or '-'}[/{color}]"
