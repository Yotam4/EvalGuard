"""Gate evaluator: ops, NaN, missing metrics, blocking semantics."""

from __future__ import annotations

import math

from evalguard_cli.local.gate import evaluate_gates


def test_basic_ops_and_passthrough() -> None:
    metrics = {"pass_rate": 1.0, "judge.mean": 4.5, "cost_usd": 0.0}
    gates = [
        {"name": "g", "blocking": True, "rules": [
            {"metric": "pass_rate",  "op": ">=", "value": 0.95},
            {"metric": "judge.mean", "op": ">=", "value": 4.0},
            {"metric": "cost_usd",   "op": "<=", "value": 1.0},
        ]},
    ]
    result = evaluate_gates(gates, metrics)
    assert len(result) == 1
    assert result[0].passed is True
    assert all(d["passed"] for d in result[0].details)


def test_failing_rule_marks_gate_failed() -> None:
    metrics = {"pass_rate": 0.5}
    result = evaluate_gates(
        [{"name": "g", "rules": [{"metric": "pass_rate", "op": ">=", "value": 0.9}]}],
        metrics,
    )
    assert result[0].passed is False
    assert result[0].blocking is True  # default


def test_missing_metric_is_treated_as_failure() -> None:
    result = evaluate_gates(
        [{"name": "g", "rules": [{"metric": "missing", "op": ">=", "value": 1.0}]}],
        metrics={},
    )
    assert result[0].passed is False
    assert math.isnan(result[0].details[0]["actual"])


def test_warn_gate_is_non_blocking() -> None:
    result = evaluate_gates(
        [{"name": "g", "blocking": False,
          "rules": [{"metric": "pass_rate", "op": ">=", "value": 0.9}]}],
        metrics={"pass_rate": 0.5},
    )
    assert result[0].blocking is False
    assert result[0].passed is False
