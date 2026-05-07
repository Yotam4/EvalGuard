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

import importlib
import operator
import time
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

# Canonical gate severities. Drift-tested against the JSON schemas.
SEVERITIES: tuple[str, ...] = ("block", "warn", "log")

# Canonical gate-status values produced by ``run_cmd`` for trials and runs.
# "none" is the sentinel for "no gates configured".
GATE_STATUSES: tuple[str, ...] = ("passed", "failed", "warned", "none")

# Canonical threshold types accepted under ``layers.<name>.threshold.type``.
# Drift-tested against ``evalguard.schema.json``.
THRESHOLD_TYPES: tuple[str, ...] = ("absolute", "relative", "ttest")

# Aggregations actually implemented by ``_resolve_aggregation``. The
# JSON schema's ``aggregation`` enum is a superset (advertises ``p50``
# / ``p95`` for forward compat); a drift test pins the gap so users
# get a clear "not implemented" error rather than silent fail-closed.
SUPPORTED_AGGREGATIONS: tuple[str, ...] = (
    "pass_rate",
    "row_pass_rate",
    "mean",
    "count_failures",
    "pass_rate_by_tag",
)


@dataclass
class GateResult:
    name: str
    blocking: bool        # True iff severity == "block"
    passed: bool
    details: list[dict[str, Any]] = field(default_factory=list)
    severity: str = "block"
    layer: int | None = None
    # Populated when the gate had a ``custom_check.module``. The
    # executor surfaces this on a dedicated ``gate.custom_check.invoked``
    # event so the python escape hatch is independently auditable.
    custom_check_execution: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# public API


def evaluate_gates(
    legacy_gates: list[dict[str, Any]] | None,
    metrics: dict[str, Any],
    *,
    layers: dict[str, dict[str, Any]] | None = None,
    baseline: dict[str, Any] | None = None,
) -> list[GateResult]:
    """Evaluate both legacy and per-layer gates against ``metrics``.

    ``baseline`` is a metrics dict from a prior run (typically a
    ``main``-branch run loaded via ``baseline_io.load_baseline``). When
    present and a per-layer gate has ``threshold.type: relative``, the
    gate compares ``actual − baseline_actual`` against
    ``min_delta_vs_baseline`` / ``max_delta_vs_baseline``. When absent,
    relative rules are skipped and only absolute rules run — the same
    YAML works with or without a baseline.
    """
    out: list[GateResult] = []
    out.extend(_evaluate_legacy(legacy_gates or [], metrics))
    out.extend(_evaluate_layers(layers or {}, metrics, baseline=baseline))
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


def _evaluate_layers(
    layers: dict[str, dict[str, Any]],
    metrics: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
) -> list[GateResult]:
    results: list[GateResult] = []
    by_evaluator = metrics.get("by_evaluator", {}) or {}
    by_layer = metrics.get("by_layer", {}) or {}
    by_tag = metrics.get("by_tag", {}) or {}

    base_by_evaluator = (baseline or {}).get("by_evaluator", {}) or {}
    base_by_layer = (baseline or {}).get("by_layer", {}) or {}
    base_by_tag = (baseline or {}).get("by_tag", {}) or {}

    for layer_name, gate in layers.items():
        idx = LAYER_INDEX.get(layer_name)
        severity = gate.get("severity", "block")
        agg = gate.get("aggregation", "pass_rate")
        threshold = gate.get("threshold") or {}
        threshold_type = threshold.get("type", "absolute")
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
            if actual is None and ("min" in threshold or "max" in threshold):
                # Disambiguate the two fail-closed modes so YAML typos
                # (unknown evaluator id) don't get blamed on the
                # aggregation enum and vice versa.
                if agg not in SUPPORTED_AGGREGATIONS:
                    err = (f"unsupported aggregation {agg!r}: not implemented "
                           f"(supported: {', '.join(SUPPORTED_AGGREGATIONS)})")
                elif evaluator_id and evaluator_id not in by_evaluator:
                    err = (f"unknown evaluator {evaluator_id!r}: not seen in "
                           f"this run's metrics — check spelling against "
                           f"the evaluators block")
                else:
                    err = (f"aggregation {agg!r} unavailable: layer "
                           f"{layer_name!r} has no scores yet")
                details.append({
                    "metric": label, "op": "available", "target": 1.0,
                    "actual": float("nan"), "passed": False,
                    "error": err,
                })
                passed = False
            elif actual is not None:
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

            # 4a) statistical thresholds — Welch's two-sample t-test on
            #     per-evaluator score samples, baseline vs current. Only
            #     fires when both sides have samples; missing samples skip
            #     non-blockingly so a 1.0.0 baseline doesn't tank a gate.
            if threshold_type == "ttest":
                if not evaluator_id:
                    details.append({
                        "metric": f"{layer_name}.ttest", "op": "config",
                        "target": float("nan"), "actual": float("nan"),
                        "passed": False,
                        "error": "ttest threshold requires 'evaluator: <id>' to scope the comparison",
                    })
                    passed = False
                else:
                    # Distinguish "evaluator not configured in the
                    # current run" (config error → fail loudly) from
                    # "configured but not enough samples" (skip
                    # non-blockingly so a fresh PR with a new evaluator
                    # doesn't tank against an old baseline).
                    cur_evals = metrics.get("by_evaluator", {}) or {}
                    if evaluator_id not in cur_evals:
                        details.append({
                            "metric":  f"{evaluator_id}.ttest",
                            "op":      "config",
                            "target":  float("nan"),
                            "actual":  float("nan"),
                            "passed":  False,
                            "error":   (
                                f"unknown evaluator {evaluator_id!r}; available: "
                                f"{sorted(cur_evals)}"
                            ),
                        })
                        passed = False
                    else:
                        cur_samples = (metrics.get("samples", {}) or {}).get(evaluator_id, [])
                        base_samples = (
                            ((baseline or {}).get("samples", {}) or {}).get(evaluator_id, [])
                            if baseline is not None else []
                        )
                        # Default ``min_n`` is 10 (was 2). Welch's
                        # t-test with df ≈ 1 has so little statistical
                        # power that any "regression" verdict is
                        # essentially noise — n=10 is the conventional
                        # floor for a nominal 80% power at α=0.05 on
                        # a moderate effect size (Cohen's d ≈ 0.8).
                        # Schema enforces ``minimum: 2`` so users can
                        # still override down for tests / niche cases.
                        min_n = int(threshold.get("min_n", 10))
                        if len(cur_samples) < min_n or len(base_samples) < min_n:
                            details.append({
                                "metric": f"{evaluator_id}.ttest",
                                "op": "skip", "target": float(min_n),
                                "actual": float(min(len(cur_samples), len(base_samples))),
                                "passed": True,
                                "reason": "insufficient samples on baseline or current side; ttest skipped",
                            })
                        else:
                            result = _welch_for_gate(
                                cur_samples, base_samples,
                                alpha=float(threshold.get("alpha", 0.05)),
                                alternative=str(threshold.get("alternative", "less")),
                            )
                            details.append(result)
                            passed = passed and result["passed"]

            # 4b) relative thresholds (Δ vs baseline). Only fires when both
            #     a baseline is provided AND the threshold type is "relative".
            if threshold_type == "relative" and baseline is not None and actual is not None:
                base_actual, _ = _resolve_aggregation(
                    agg, layer_name, idx, evaluator_id,
                    by_evaluator=base_by_evaluator,
                    by_layer=base_by_layer,
                    by_tag=base_by_tag,
                )
                if base_actual is not None:
                    delta = actual - base_actual
                    if "min_delta_vs_baseline" in threshold:
                        target = float(threshold["min_delta_vs_baseline"])
                        ok = delta >= target
                        details.append({
                            "metric": f"{label}.delta_vs_baseline",
                            "op": ">=", "target": target,
                            "actual": delta,
                            "baseline": base_actual, "passed": ok,
                        })
                        passed = passed and ok
                    if "max_delta_vs_baseline" in threshold:
                        target = float(threshold["max_delta_vs_baseline"])
                        ok = delta <= target
                        details.append({
                            "metric": f"{label}.delta_vs_baseline",
                            "op": "<=", "target": target,
                            "actual": delta,
                            "baseline": base_actual, "passed": ok,
                        })
                        passed = passed and ok

        # 4) custom_check Python escape hatch
        custom_execution: dict[str, Any] | None = None
        if (custom := gate.get("custom_check")):
            module_path = custom.get("module", "")
            cfg = dict(custom.get("config") or {})
            t0 = time.monotonic()
            try:
                result = _invoke_custom_check(custom, metrics)
            except Exception as e:  # noqa: BLE001
                duration_ms = int((time.monotonic() - t0) * 1000)
                custom_execution = {
                    "module":      module_path,
                    "config":      cfg,
                    "passed":      False,
                    "details":     [],
                    "error":       f"{type(e).__name__}: {e}",
                    "duration_ms": duration_ms,
                }
                details.append({
                    "metric": f"{layer_name}.custom_check",
                    "op": "raise", "target": 0.0, "actual": float("nan"),
                    "passed": False, "error": str(e),
                })
                passed = False
            else:
                duration_ms = int((time.monotonic() - t0) * 1000)
                custom_execution = {
                    "module":      module_path,
                    "config":      cfg,
                    "passed":      bool(result.get("passed", True)),
                    "details":     list(result.get("details", [])),
                    "error":       None,
                    "duration_ms": duration_ms,
                }
                passed = passed and bool(result.get("passed", True))
                for d in result.get("details", []):
                    d.setdefault("metric", f"{layer_name}.custom_check")
                    details.append(d)

        results.append(GateResult(
            name=layer_name,
            blocking=(severity == "block"),
            severity=severity,
            passed=passed,
            details=details,
            layer=idx,
            custom_check_execution=custom_execution,
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
        # Use the canonical ``fail_count`` from the by_layer rollup
        # (computed as ``SUM(passed=0)`` over score rows). Previously
        # we derived it as ``n - n*pass_rate``, which is the same
        # number in floats but in different units from the per-
        # evaluator branch above — pick one source of truth.
        return float(layer_data.get("fail_count", 0)), label
    return None, label


def _invoke_custom_check(spec: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``module: foo.bar:func`` (or ``foo.bar.func``) and invoke it.

    The function is given the full metrics dict and the gate's ``config``.
    It must return ``{"passed": bool, "details": [{...}]}``. Any raised
    exception is caught by the caller and surfaced as a gate detail.
    """
    path = spec["module"]
    if ":" in path:
        mod_name, func_name = path.rsplit(":", 1)
    else:
        mod_name, func_name = path.rsplit(".", 1)
    module = importlib.import_module(mod_name)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise AttributeError(f"{path} did not resolve to a callable")
    out = func(metrics, dict(spec.get("config") or {}))
    if not isinstance(out, dict) or "passed" not in out:
        raise TypeError(
            f"{path} must return a dict with at least a 'passed' key; got {type(out).__name__}"
        )
    return out


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


def _welch_for_gate(
    current: list[float], baseline: list[float],
    *, alpha: float, alternative: str,
) -> dict[str, Any]:
    """Run Welch's t-test and shape its result into a gate-detail dict.

    ``alternative`` controls the directionality of the test:

    - ``less``       — fail if current < baseline at significance ``alpha``.
                       This is the canonical regression-detection use case.
    - ``greater``    — fail if current > baseline (e.g. cost / latency).
    - ``two_sided``  — fail on any significant difference.
    """
    # Imported lazily so the dependency-free CLI import path stays fast.
    from evalguard_cli.local.stats import welchs_t_test

    res = welchs_t_test(current, baseline)
    if alternative == "less":
        p = res.p_less
    elif alternative == "greater":
        p = res.p_greater
    else:
        alternative = "two_sided"
        p = res.p_two_sided
    significant = p < alpha
    # "Pass" means we did NOT detect a significant regression in the
    # bad direction. Operators want a green gate when the new run is
    # not statistically worse.
    passed = not significant
    return {
        "metric":        "ttest.p_value",
        "op":            "<", "target": float(alpha),
        "actual":        float(p),
        "passed":        passed,
        "alternative":   alternative,
        "t_stat":        float(res.t_stat),
        "dof":           float(res.dof),
        "n_current":     int(res.n1),
        "n_baseline":    int(res.n2),
        "mean_current":  float(res.mean1),
        "mean_baseline": float(res.mean2),
        "delta_mean":    float(res.mean1 - res.mean2),
    }


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
