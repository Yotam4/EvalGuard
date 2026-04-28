"""Render a run summary to the terminal."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table

# Structured sub-dicts produced by ``compute_metrics``. Useful for gate
# evaluation but too noisy to dump straight into the run-summary table.
_STRUCTURED_KEYS = {"by_evaluator", "by_layer", "by_tag"}


def render_run_table(run_record: Any, metrics: dict[str, Any], *, console: Console) -> None:
    table = Table(title=f"Run summary — {run_record.run_id}")
    table.add_column("metric", style="cyan", no_wrap=True)
    table.add_column("value", justify="right")
    for k in sorted(metrics):
        if k in _STRUCTURED_KEYS:
            continue
        v = metrics[k]
        if isinstance(v, float):
            table.add_row(k, f"{v:.4f}")
        else:
            table.add_row(k, str(v))
    console.print(table)
