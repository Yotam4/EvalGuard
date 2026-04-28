"""Render a run summary to the terminal."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table


def render_run_table(run_record: Any, metrics: dict[str, float], *, console: Console) -> None:
    table = Table(title=f"Run summary — {run_record.run_id}")
    table.add_column("metric", style="cyan", no_wrap=True)
    table.add_column("value", justify="right")
    for k in sorted(metrics):
        v = metrics[k]
        if isinstance(v, float):
            table.add_row(k, f"{v:.4f}")
        else:
            table.add_row(k, str(v))
    console.print(table)
