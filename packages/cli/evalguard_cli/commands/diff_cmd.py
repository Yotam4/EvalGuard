"""``evalguard diff`` — side-by-side comparison of two local runs.

Pure debugging tool — no exit-code teeth. To gate CI on Δ, configure a
``threshold.type: relative`` gate in ``evalguard.yaml`` and pass
``--baseline`` to ``evalguard run``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from evalguard_cli.console import console
from evalguard_cli.local.sqlite_store import SqliteStore


def diff(
    run_a: str = typer.Argument(..., help="Older / baseline run id (or prefix)."),
    run_b: str = typer.Argument(..., help="Newer / candidate run id (or prefix)."),
    db_path: Path = typer.Option(Path(".evalguard/local.db"), "--db", help="SQLite path"),
) -> None:
    """Render a side-by-side metrics diff between two runs in the local store."""
    if not db_path.exists():
        console.print(f"[yellow]no run history at {db_path}[/yellow]")
        raise typer.Exit(1)
    store = SqliteStore(db_path)

    runs = store.list_runs(limit=1000)
    a = next((r for r in runs if r["run_id"].startswith(run_a)), None)
    b = next((r for r in runs if r["run_id"].startswith(run_b)), None)
    if a is None:
        console.print(f"[red]run not found:[/red] {run_a}")
        raise typer.Exit(1)
    if b is None:
        console.print(f"[red]run not found:[/red] {run_b}")
        raise typer.Exit(1)

    metrics_a = store.compute_metrics(a["run_id"])
    metrics_b = store.compute_metrics(b["run_id"])

    console.print(
        f"[bold]A[/bold] [cyan]{a['run_id']}[/cyan]  "
        f"[dim]{a.get('started_at')} · status={a.get('status')}[/dim]"
    )
    console.print(
        f"[bold]B[/bold] [cyan]{b['run_id']}[/cyan]  "
        f"[dim]{b.get('started_at')} · status={b.get('status')}[/dim]"
    )

    rows = _build_diff_rows(metrics_a, metrics_b)
    if not rows:
        console.print("[yellow]no scalar metrics to diff.[/yellow]")
        return

    table = Table(title="Run diff (B − A)")
    table.add_column("metric", style="cyan")
    table.add_column("A", justify="right")
    table.add_column("B", justify="right")
    table.add_column("Δ", justify="right")
    for metric, va, vb, delta, sign in rows:
        delta_s = f"{delta:+.4f}" if delta is not None else "-"
        if sign == "regress":
            delta_s = f"[red]{delta_s} ✗[/red]"
        elif sign == "improve":
            delta_s = f"[green]{delta_s}[/green]"
        table.add_row(
            metric,
            "-" if va is None else f"{va:.4f}",
            "-" if vb is None else f"{vb:.4f}",
            delta_s,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Diff math


# Metrics where *lower* is better — a lower B means the candidate run
# "improved" (e.g. cheaper, faster). Heuristic: name contains "cost" or
# "latency" or starts with "fail".
def _lower_is_better(name: str) -> bool:
    lo = name.lower()
    return ("cost" in lo) or ("latency" in lo) or lo.startswith("fail") or lo.endswith("fail_count")


def _build_diff_rows(
    metrics_a: dict[str, Any],
    metrics_b: dict[str, Any],
) -> list[tuple[str, float | None, float | None, float | None, str]]:
    """Build the diff table rows, sorted with regressions first."""
    keys = sorted(_scalar_keys(metrics_a) | _scalar_keys(metrics_b))
    rows: list[tuple[str, float | None, float | None, float | None, str]] = []
    for key in keys:
        va = _scalar(metrics_a, key)
        vb = _scalar(metrics_b, key)
        delta = (vb - va) if va is not None and vb is not None else None
        sign = "neutral"
        if delta is not None and abs(delta) > 1e-9:
            improved = (delta < 0) if _lower_is_better(key) else (delta > 0)
            sign = "improve" if improved else "regress"
        rows.append((key, va, vb, delta, sign))
    rows.sort(key=lambda r: (0 if r[4] == "regress" else 1, r[0]))
    return rows


def _scalar_keys(metrics: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for k, v in metrics.items():
        if isinstance(v, (int, float)) and k not in {"row_count"}:
            out.add(k)
    # by_evaluator and by_layer hold scalar means / pass_rates worth diffing.
    for ev_id, agg in (metrics.get("by_evaluator") or {}).items():
        for inner in ("mean", "pass_rate", "fail_count"):
            if inner in agg:
                out.add(f"{ev_id}.{inner}")
    for layer_idx, agg in (metrics.get("by_layer") or {}).items():
        for inner in ("mean", "pass_rate", "row_pass_rate"):
            if inner in agg:
                out.add(f"layer{layer_idx}.{inner}")
    return out


def _scalar(metrics: dict[str, Any], dotted: str) -> float | None:
    if dotted in metrics:
        v = metrics[dotted]
        return float(v) if isinstance(v, (int, float)) else None
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        if head.startswith("layer") and head[5:].isdigit():
            agg = (metrics.get("by_layer") or {}).get(int(head[5:]))
            if agg is None:
                agg = (metrics.get("by_layer") or {}).get(head[5:])
            if isinstance(agg, dict) and tail in agg:
                v = agg[tail]
                return float(v) if isinstance(v, (int, float)) else None
        agg = (metrics.get("by_evaluator") or {}).get(head)
        if isinstance(agg, dict) and tail in agg:
            v = agg[tail]
            return float(v) if isinstance(v, (int, float)) else None
    return None
