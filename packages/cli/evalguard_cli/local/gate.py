"""Gate evaluation.

Two shapes are accepted simultaneously, and a single ``GateResult`` list is
produced for either.

1. **Legacy global gates** (``gates: [{name, rules: [{metric, op, value}]}]``).
   Kept for back-compat with Phase-0 configs.

2. **Per-layer gates** (``layers: { heuristics: {severity, threshold, ...} }``).
   Each pyramid step (heuristics / metrics / judge_offline / judge_online /
   human) gets an independently controllable gate with one of three
   severities (``block`` / ``warn`` / ``log``) and an aggregation that
   targets either the layer roll-up or a specific evaluator's metric.

Severity → CLI behaviour:
    block — non-zero exit, "FAIL" in PR comment
    warn  — exit 0 but emits a warning row in PR comment
    log   — recorded only; never surfaces in PR/CI status
"""

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

# Stable layer-name → integer mapping. Mirrors the pyramid ordering used by
# the executor when it decides which layers to run / short-circuit.
LAYER_INDEX: dict[str, int] = {
    "heuristics":    1,
    "metrics":       2,
    "judge_offline": 3,
    "judge_online":  4,
    "human":         5,
}


@dataclass
class GateResult:
    name: str
    blocking: bool        # True iff severity == "block"
    passed: bool
    details: list[dict[str, Any]] = field(default_factory=list)
    severity: str = "block"
    layer: int | None = None


# ---------------------------------------------------------------------------
# public API


def evaluate_gates(
    legacy_gates: list[dict[str, Any]] | None,
    metrics: dict[str, Any],
    *,
    layers: dict[str, dict[str, Any]] | None = None,
) -> list[GateResult]:
    """Evaluate both legacy and per-layer gates against ``metrics``."""
    out: list[GateResult] = []
    out.extend(_evaluate_legacy(legacy_gates or [], metrics))
    out.extend(_evaluate_layers(layers or {}, metrics))
    return out


# ---------------------------------------------------------------------------
# legacy shape


def _evaluate_legacy(gates: list[dict[str, Any]], metrics: dict[str, Any]) -> list[GateResult]:
    results: list[GateResult] = []
    for gate in gates:
        # `severity` overrides `blocking` if set; otherwise blocking=True ⇒ "block".
        severity = gate.get("severity") or ("block" if gate.get("blocking", True) else "warn")
        details: list[dict[str, Any]] = []
        all_passed = True
        for rule in gate.get("rules", []):
            actual = _coerce_float(metrics.get(rule["metric"]))
            target = float(rule["value"])
            ok = _apply_op(rule["op"], actual, target)
            details.append({
                "metric": rule["metric"], "op": rule["op"],
                "target": target, "actual": actual, "passed": ok,
            })
            all_passed = all_passed and ok
        results.append(GateResult(
            name=gate["name"],
            blocking=(severity == "block"),
            severity=severity,
            passed=all_passed,
            details=details,
        ))
    return results


# ---------------------------------------------------------------------------
# per-layer shape


def _evaluate_layers(layers: dict[str, dict[str, Any]], metrics: dict[str, Any]) -> list[GateResult]:
    results: list[GateResult] = []
    by_evaluator = metrics.get("by_evaluator", {}) or {}
    by_layer = metrics.get("by_layer", {}) or {}
    by_tag = metrics.get("by_tag", {}) or {}

    for layer_name, gate in layers.items():
        idx = LAYER_INDEX.get(layer_name)
        severity = gate.get("severity", "block")
        agg = gate.get("aggregation", "pass_rate")
        threshold = gate.get("threshold") or {}
        evaluator_id = gate.get("evaluator")

        details: list[dict[str, Any]] = []
        passed = True

        # 1) `rules:` (same shape as legacy) are evaluated first if present.
        for rule in gate.get("rules", []) or []:
            actual = _coerce_float(metrics.get(rule["metric"]))
            target = float(rule["value"])
            ok = _apply_op(rule["op"], actual, target)
            details.append({
                "metric": rule["metric"], "op": rule["op"],
                "target": target, "actual": actual, "passed": ok,
            })
            passed = passed and ok

        # 2) `aggregation` + `threshold.{min,max}` evaluation.
        if threshold:
            actual, label = _resolve_aggregation(
                agg, layer_name, idx, evaluator_id,
                by_evaluator=by_evaluator, by_layer=by_layer, by_tag=by_tag,
            )
            if actual is not None:
                if "min" in threshold:
                    ok = _apply_op(">=", actual, float(threshold["min"]))
                    details.append({
                        "metric": label, "op": ">=", "target": float(threshold["min"]),
                        "actual": actual, "passed": ok,
                    })
                    passed = passed and ok
                if "max" in threshold:
                    ok = _apply_op("<=", actual, float(threshold["max"]))
                    details.append({
                        "metric": label, "op": "<=", "target": float(threshold["max"]),
                        "actual": actual, "passed": ok,
                    })
                    passed = passed and ok

            # 3) per_tag_overrides on a pass_rate_by_tag aggregation
            for tag, tag_min in (threshold.get("per_tag_overrides") or {}).items():
                tag_actual = float((by_tag.get(tag) or {}).get("pass_rate", 0.0))
                ok = tag_actual >= float(tag_min)
                details.append({
                    "metric": f"{layer_name}.tag[{tag}].pass_rate",
                    "op": ">=", "target": float(tag_min),
                    "actual": tag_actual, "passed": ok,
                })
                passed = passed and ok

        results.append(GateResult(
            name=layer_name,
            blocking=(severity == "block"),
            severity=severity,
            passed=passed,
            details=details,
            layer=idx,
        ))

    return results


def _resolve_aggregation(
    agg: str,
    layer_name: str,
    layer_idx: int | None,
    evaluator_id: str | None,
    *,
    by_evaluator: dict[str, dict[str, Any]],
    by_layer: dict[Any, dict[str, Any]],
    by_tag: dict[str, dict[str, Any]],
) -> tuple[float | None, str]:
    """Compute the actual number for one aggregation, plus a display label."""
    # Scope to a specific evaluator if given.
    if evaluator_id:
        agg_data = by_evaluator.get(evaluator_id)
        if agg_data is None:
            return None, f"{evaluator_id}.{agg}"
        if agg in {"mean", "pass_rate"}:
            return float(agg_data.get(agg, 0.0)), f"{evaluator_id}.{agg}"
        if agg == "count_failures":
            return float(agg_data.get("fail_count", 0)), f"{evaluator_id}.count_failures"
        return None, f"{evaluator_id}.{agg}"

    # Layer roll-up. by_layer keys may arrive as int (in-process) or str (JSON-loaded).
    layer_data = by_layer.get(layer_idx) or by_layer.get(str(layer_idx)) or {}
    label = f"{layer_name}.{agg}"
    if agg == "pass_rate":
        return float(layer_data.get("pass_rate", 0.0)), label
    if agg == "row_pass_rate":
        return float(layer_data.get("row_pass_rate", 0.0)), label
    if agg == "mean":
        return float(layer_data.get("mean", 0.0)), label
    if agg == "pass_rate_by_tag":
        # Aggregate baseline = mean across tags. Per-tag thresholds are
        # applied separately via per_tag_overrides.
        if not by_tag:
            return None, label
        rates = [float(b.get("pass_rate", 0.0)) for b in by_tag.values()]
        return (sum(rates) / len(rates)) if rates else 0.0, label
    if agg == "count_failures":
        return float(layer_data.get("n", 0)) - float(layer_data.get("n", 0)) * float(
            layer_data.get("pass_rate", 0.0)
        ), label
    return None, label


def _apply_op(op: str, actual: float, target: float) -> bool:
    if actual != actual:  # NaN
        return False
    return _OPS[op](actual, target)


def _coerce_float(value: Any) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# rendering


def format_gate_report(results: list[GateResult]) -> Table:
    table = Table(title="Gates")
    table.add_column("gate", style="cyan", no_wrap=True)
    table.add_column("sev", justify="center")
    table.add_column("metric")
    table.add_column("op", justify="center")
    table.add_column("target", justify="right")
    table.add_column("actual", justify="right")
    table.add_column("status")
    for g in results:
        sev_color = {"block": "red", "warn": "yellow", "log": "dim"}.get(g.severity, "white")
        sev_cell = f"[{sev_color}]{g.severity}[/{sev_color}]"
        if not g.details:
            table.add_row(g.name, sev_cell, "-", "-", "-", "-",
                          "[green]PASS[/green]" if g.passed else "[red]FAIL[/red]")
            continue
        for d in g.details:
            actual = d["actual"]
            actual_s = "nan" if actual != actual else f"{actual:.4f}"
            status = "[green]PASS[/green]" if d["passed"] else (
                "[red]FAIL[/red]" if g.severity == "block"
                else f"[{sev_color}]{g.severity.upper()}[/{sev_color}]"
            )
            table.add_row(g.name, sev_cell, d["metric"], d["op"],
                          f"{d['target']:.4f}", actual_s, status)
    return table
