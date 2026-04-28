"""``evalguard run`` — execute the eval defined by ``evalguard.yaml``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from evalguard_cli.console import console
from evalguard_cli.local.gate import evaluate_gates, format_gate_report
from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.report import render_run_table
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config

EXIT_PASS = 0
EXIT_GATE_FAIL = 2
EXIT_INFRA_ERROR = 1


def run(
    config: Path = typer.Option(Path("evalguard.yaml"), "--config", "-c", help="Path to evalguard.yaml"),
    db_path: Path = typer.Option(Path(".evalguard/local.db"), "--db", help="SQLite path for run history"),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Abort on the first failing row"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress per-row output"),
) -> None:
    """Run the eval pipeline against the local executor."""
    try:
        cfg = load_config(config)
    except Exception as e:  # noqa: BLE001 - user-facing surface
        console.print(f"[red]config error:[/red] {e}")
        raise typer.Exit(EXIT_INFRA_ERROR) from e

    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db_path)
    store.init_schema()

    try:
        run_record = asyncio.run(execute(cfg, store=store, fail_fast=fail_fast, quiet=quiet))
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]run error:[/red] {e}")
        raise typer.Exit(EXIT_INFRA_ERROR) from e

    metrics = store.compute_metrics(run_record.run_id)
    render_run_table(run_record, metrics, console=console)

    gates = cfg.raw.get("gates") or []
    gate_results = evaluate_gates(gates, metrics)
    store.save_gate_results(run_record.run_id, gate_results)
    if gate_results:
        console.print()
        console.print(format_gate_report(gate_results))

    blocking_failed = any(g.passed is False and g.blocking for g in gate_results)
    warned = any(g.passed is False and not g.blocking for g in gate_results)
    gate_status = "failed" if blocking_failed else ("warned" if warned else ("passed" if gate_results else "none"))

    if run_record.row_status == "cost_capped":
        overall = "cost_capped"
    elif run_record.row_status == "failed":
        overall = "row_failed"
    elif blocking_failed:
        overall = "gate_failed"
    elif warned:
        overall = "warned"
    else:
        overall = "passed"
    store.finalize_run(run_record.run_id, status=overall, gate_status=gate_status)

    console.print(f"\n[bold]overall:[/bold] {_pretty(overall)}")
    raise typer.Exit(EXIT_GATE_FAIL if blocking_failed else EXIT_PASS)


def _pretty(status: str) -> str:
    color = {
        "passed":     "green",
        "warned":     "yellow",
        "row_failed": "red",
        "gate_failed":"red",
        "cost_capped":"yellow",
    }.get(status, "white")
    return f"[{color}]{status}[/{color}]"
