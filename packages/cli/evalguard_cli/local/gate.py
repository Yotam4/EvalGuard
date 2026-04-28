"""Gate evaluation: turn the YAML rules into pass/fail with diagnostics."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.table import Table

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">":  operator.gt,
    "<":  operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass
class GateResult:
    name: str
    blocking: bool
    passed: bool
    details: list[dict[str, Any]] = field(default_factory=list)


def evaluate_gates(gates: list[dict[str, Any]], metrics: dict[str, float]) -> list[GateResult]:
    out: list[GateResult] = []
    for gate in gates:
        details: list[dict[str, Any]] = []
        all_passed = True
        for rule in gate.get("rules", []):
            metric = rule["metric"]
            op = rule["op"]
            target = float(rule["value"])
            actual = float(metrics.get(metric, float("nan")))
            cmp = _OPS[op]
            ok = False if actual != actual else cmp(actual, target)  # NaN -> fail
            details.append({
                "metric": metric,
                "op": op,
                "target": target,
                "actual": actual,
                "passed": ok,
            })
            all_passed = all_passed and ok
        out.append(GateResult(
            name=gate["name"],
            blocking=bool(gate.get("blocking", True)),
            passed=all_passed,
            details=details,
        ))
    return out


def format_gate_report(results: list[GateResult]) -> Table:
    table = Table(title="Gates")
    table.add_column("gate", style="cyan", no_wrap=True)
    table.add_column("metric")
    table.add_column("op", justify="center")
    table.add_column("target", justify="right")
    table.add_column("actual", justify="right")
    table.add_column("status")
    for g in results:
        for d in g.details:
            status = "[green]PASS[/green]" if d["passed"] else "[red]FAIL[/red]"
            actual = d["actual"]
            actual_s = "nan" if actual != actual else f"{actual:.4f}"
            table.add_row(
                g.name + ("" if g.blocking else " (warn)"),
                d["metric"],
                d["op"],
                f"{d['target']:.4f}",
                actual_s,
                status,
            )
    return table
