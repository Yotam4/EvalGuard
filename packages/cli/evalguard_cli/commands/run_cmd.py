"""``evalguard run`` — execute the eval defined by ``evalguard.yaml``.

Multi-trial pipeline:

1. ``execute()`` runs every (provider × prompt) trial; each writes its
   own rows + scores keyed by ``trial_id``.
2. For each trial, gates are evaluated against trial-scoped metrics and
   recorded with ``trial_id`` set.
3. The run-level verdict combines trial verdicts according to
   ``gate_strategy`` (``all`` → every trial must pass any blocking
   gate; ``any`` → at least one trial must pass).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import typer
from rich.table import Table

from evalguard_cli.console import console
from evalguard_cli.local.gate import GateResult, evaluate_gates, format_gate_report
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
    except Exception as e:  # noqa: BLE001 — user-facing surface
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

    legacy_gates = cfg.raw.get("gates") or []
    layer_gates = cfg.raw.get("layers") or {}
    gate_strategy = cfg.raw.get("gate_strategy", "all")
    audit = run_record.audit

    # Map gate.name → resolved spec so each gate.evaluated event records
    # the full pass/fail criteria the user set for this run (severity,
    # aggregation, threshold, per_tag_overrides, evaluator scope,
    # custom_check), not just the resolved verdict.
    gate_specs: dict[str, dict] = {**(layer_gates or {})}
    for legacy in legacy_gates:
        gate_specs[legacy["name"]] = legacy

    # Per-trial gate evaluation
    trial_verdicts: list[dict] = []
    for trial in run_record.trials:
        trial_metrics = store.compute_metrics(run_record.run_id, trial_id=trial.trial_id)
        trial_gates = evaluate_gates(legacy_gates, trial_metrics, layers=layer_gates)
        store.save_gate_results(run_record.run_id, trial_gates, trial_id=trial.trial_id)

        # Audit: one event per gate evaluated. ``spec`` records the
        # criteria the user set; ``details`` records what each rule
        # actually evaluated to. Both shapes are needed for forensics —
        # "what did the gate require" vs "what did it observe".
        if audit is not None:
            for g in trial_gates:
                spec = gate_specs.get(g.name) or {}
                # Pre-allocate the gate event's span so a dedicated
                # ``gate.custom_check.invoked`` child event can record
                # the parent_span_id when the gate ran a Python check.
                gate_span_id = uuid.uuid4().hex[:16]
                audit.emit(
                    "gate.evaluated",
                    trial_id=trial.trial_id,
                    span_id=gate_span_id,
                    subject_id=g.name,
                    inputs=g.details,
                    outputs={"passed": g.passed, "severity": g.severity},
                    payload={
                        # What the gate said happened
                        "gate_name": g.name,
                        "severity":  g.severity,
                        "blocking":  g.blocking,
                        "passed":    g.passed,
                        "layer":     g.layer,
                        "details":   g.details,
                        # What the gate was configured to require
                        "spec":             spec,
                        "aggregation":      spec.get("aggregation"),
                        "threshold":        spec.get("threshold"),
                        "evaluator_scope":  spec.get("evaluator"),
                        "custom_check":     spec.get("custom_check"),
                        "rules":            spec.get("rules"),
                    },
                )
                # If the gate ran a custom Python check, record its
                # execution as a separate child event. Module path,
                # config, duration, pass/fail, and any exception live
                # here so the escape hatch is independently auditable.
                if g.custom_check_execution is not None:
                    exec_meta = g.custom_check_execution
                    audit.emit(
                        "gate.custom_check.invoked",
                        trial_id=trial.trial_id,
                        parent_span_id=gate_span_id,
                        subject_id=exec_meta["module"],
                        outputs={"passed": exec_meta["passed"]},
                        payload=exec_meta,
                        duration_ms=exec_meta.get("duration_ms"),
                    )

        trial_blocking_failed = any(g.passed is False and g.severity == "block" for g in trial_gates)
        trial_warned = any(g.passed is False and g.severity == "warn" for g in trial_gates)
        gate_status = (
            "failed" if trial_blocking_failed
            else "warned" if trial_warned
            else "passed" if trial_gates
            else "none"
        )
        if trial.row_status == "cost_capped":
            trial_overall = "cost_capped"
        elif trial.row_status == "failed":
            trial_overall = "row_failed"
        elif trial_blocking_failed:
            trial_overall = "gate_failed"
        elif trial_warned:
            trial_overall = "warned"
        else:
            trial_overall = "passed"
        store.finalize_trial(trial.trial_id, status=trial_overall, gate_status=gate_status)
        trial_verdicts.append({
            "trial": trial,
            "metrics": trial_metrics,
            "gates": trial_gates,
            "status": trial_overall,
            "gate_status": gate_status,
            "blocking_failed": trial_blocking_failed,
            "warned": trial_warned,
        })

    # Run-level aggregate render
    if len(run_record.trials) >= 2:
        _render_trials_summary(run_record, trial_verdicts, store)
    else:
        # Single-trial fallback: behave like Phase 0.5 — one summary table + gates.
        metrics = store.compute_metrics(run_record.run_id)
        render_run_table(run_record, metrics, console=console)
        if trial_verdicts:
            console.print()
            console.print(format_gate_report(trial_verdicts[0]["gates"]))

    # gate_strategy decides the run-level blocking verdict.
    if not trial_verdicts:
        run_blocking_failed = False
        run_warned = False
    elif gate_strategy == "any":
        # Pass if ANY trial passed without a blocking failure.
        run_blocking_failed = all(v["blocking_failed"] for v in trial_verdicts)
        run_warned = any(v["warned"] for v in trial_verdicts) and not run_blocking_failed
    else:  # "all"
        run_blocking_failed = any(v["blocking_failed"] for v in trial_verdicts)
        run_warned = any(v["warned"] for v in trial_verdicts) and not run_blocking_failed

    if run_blocking_failed:
        gate_status_overall = "failed"
    elif run_warned:
        gate_status_overall = "warned"
    elif trial_verdicts:
        gate_status_overall = "passed"
    else:
        gate_status_overall = "none"

    if run_record.row_status == "cost_capped":
        overall = "cost_capped"
    elif run_record.row_status == "failed" and gate_strategy == "all":
        overall = "row_failed"
    elif run_blocking_failed:
        overall = "gate_failed"
    elif run_warned:
        overall = "warned"
    else:
        overall = "passed"

    store.finalize_run(run_record.run_id, status=overall, gate_status=gate_status_overall)

    if audit is not None:
        audit.emit(
            "run.finalized",
            subject_id=run_record.project,
            payload={
                "status":         overall,
                "gate_status":    gate_status_overall,
                "row_status":     run_record.row_status,
                "row_count":      run_record.row_count,
                "row_pass_count": run_record.row_pass_count,
                "row_fail_count": run_record.row_fail_count,
                "cost_usd":       run_record.cost_usd,
                "trial_count":    len(run_record.trials),
                "gate_strategy":  gate_strategy,
            },
            cost_usd=run_record.cost_usd,
        )

    console.print(
        f"\n[bold]overall:[/bold] {_pretty(overall)}  "
        f"[dim](strategy: {gate_strategy}, {len(run_record.trials)} trial(s))[/dim]"
    )
    raise typer.Exit(EXIT_GATE_FAIL if run_blocking_failed else EXIT_PASS)


def _render_trials_summary(run_record, trial_verdicts: list[dict], store: SqliteStore) -> None:
    table = Table(title=f"Trials — {run_record.run_id}")
    table.add_column("trial",      style="cyan", no_wrap=True)
    table.add_column("provider",   style="dim")
    table.add_column("model",      no_wrap=True)
    table.add_column("status",     justify="center")
    table.add_column("rows",       justify="right")
    table.add_column("cost",       justify="right")
    table.add_column("gates",      justify="center")
    for v in trial_verdicts:
        t = v["trial"]
        rows_label = f"{t.row_pass_count}/{t.row_count}"
        gates_status = "[green]PASS[/green]" if v["gate_status"] == "passed" else (
            "[yellow]warn[/yellow]" if v["gate_status"] == "warned"
            else "[red]FAIL[/red]" if v["gate_status"] == "failed"
            else "[dim]-[/dim]"
        )
        table.add_row(
            t.trial_id, t.provider, t.model,
            _pretty(v["status"]),
            rows_label, f"${t.cost_usd:.4f}", gates_status,
        )
    console.print(table)

    # Cross-trial winner table for the metrics that matter most
    comparison = store.compute_comparison(run_record.run_id)
    if comparison.get("best_by"):
        cmp_table = Table(title="Best by metric", show_lines=False)
        cmp_table.add_column("metric", style="cyan")
        cmp_table.add_column("winner", style="green")
        cmp_table.add_column("value", justify="right")
        cmp_table.add_column("runner-up", style="dim")
        cmp_table.add_column("Δ", justify="right")
        # Curate which keys to surface — full dict goes in --json output.
        priority = ["pass_rate", "cost_usd", "row_pass_count"]
        priority += sorted(k for k in comparison["best_by"] if k.startswith("layer") and k.endswith(".mean"))
        priority += sorted(k for k in comparison["best_by"]
                           if k.endswith(".mean") and not k.startswith("layer") and k not in priority)
        seen = set()
        for key in priority:
            if key not in comparison["best_by"] or key in seen:
                continue
            seen.add(key)
            row = comparison["best_by"][key]
            delta = row["winner"]["value"] - row["runner_up"]["value"]
            cmp_table.add_row(
                key,
                row["winner"]["provider_id"],
                f"{row['winner']['value']:.4f}",
                row["runner_up"]["provider_id"],
                f"{delta:+.4f}",
            )
        console.print(cmp_table)


def _pretty(status: str) -> str:
    color = {
        "passed":     "green",
        "warned":     "yellow",
        "row_failed": "red",
        "gate_failed":"red",
        "cost_capped":"yellow",
    }.get(status, "white")
    return f"[{color}]{status}[/{color}]"
