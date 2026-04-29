"""Custom Python gates (the ``custom_check`` escape hatch)."""

from __future__ import annotations

import hashlib
import sys
import textwrap
from pathlib import Path

from evalguard_cli.local.gate import evaluate_gates


def _install_module(source: str, tmp_path: Path) -> str:
    """Drop a unique-named flat module into ``tmp_path`` and add it to ``sys.path``.

    Module names are content-hashed so each test gets its own entry in
    ``sys.modules`` instead of stepping on a shared package cache.
    """
    name = "ev_" + hashlib.md5(textwrap.dedent(source).encode()).hexdigest()[:8]
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source))
    sys.path.insert(0, str(tmp_path))
    return name


def _metrics() -> dict:
    return {
        "row_count": 3.0, "cost_usd": 0.0, "pass_rate": 1.0,
        "by_evaluator": {}, "by_layer": {}, "by_tag": {},
    }


def test_custom_check_passes_when_function_returns_true(tmp_path: Path) -> None:
    mod = _install_module("""
        def always_ok(metrics, config):
            return {"passed": True, "details": [
                {"metric": "custom.always", "op": "==", "target": 1.0, "actual": 1.0, "passed": True}
            ]}
    """, tmp_path)
    layers = {"human": {
        "severity": "log",
        "custom_check": {"module": f"{mod}:always_ok"},
    }}
    [g] = evaluate_gates(None, _metrics(), layers=layers)
    assert g.passed
    assert any(d["metric"] == "custom.always" for d in g.details)


def test_custom_check_fails_propagate(tmp_path: Path) -> None:
    mod = _install_module("""
        def reject(metrics, config):
            return {"passed": False, "details": [
                {"metric": "custom.reject", "op": "==", "target": 1.0, "actual": 0.0, "passed": False}
            ]}
    """, tmp_path)
    layers = {"judge_offline": {
        "severity": "block",
        "custom_check": {"module": f"{mod}.reject"},   # dotted form
    }}
    [g] = evaluate_gates(None, _metrics(), layers=layers)
    assert not g.passed
    assert g.severity == "block"


def test_custom_check_uses_config_dict(tmp_path: Path) -> None:
    mod = _install_module("""
        def threshold_check(metrics, config):
            min_n = float(config["min_rows"])
            actual = float(metrics["row_count"])
            return {"passed": actual >= min_n,
                    "details": [{"metric": "rows", "op": ">=",
                                 "target": min_n, "actual": actual,
                                 "passed": actual >= min_n}]}
    """, tmp_path)
    layers = {"heuristics": {
        "severity": "block",
        "custom_check": {
            "module": f"{mod}:threshold_check",
            "config": {"min_rows": 5},
        },
    }}
    [g] = evaluate_gates(None, _metrics(), layers=layers)
    assert not g.passed   # 3 < 5


def test_custom_check_exception_marks_gate_failed(tmp_path: Path) -> None:
    mod = _install_module("""
        def boom(metrics, config):
            raise RuntimeError("intentional failure")
    """, tmp_path)
    layers = {"human": {
        "severity": "block",
        "custom_check": {"module": f"{mod}:boom"},
    }}
    [g] = evaluate_gates(None, _metrics(), layers=layers)
    assert not g.passed
    assert any("intentional" in (d.get("error") or "") for d in g.details)
