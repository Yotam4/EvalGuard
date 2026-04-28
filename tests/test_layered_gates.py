"""Per-layer gates: severity, aggregation, per_tag_overrides, run_mode."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evalguard_cli.local.gate import LAYER_INDEX, evaluate_gates
from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


# ---------------------------------------------------------------------------
# gate evaluator unit tests


def _metrics(*, layer1_pass: float = 1.0, helpfulness_mean: float = 4.5,
             tags: dict[str, float] | None = None) -> dict:
    return {
        "row_count": 5.0,
        "cost_usd": 0.0,
        "pass_rate": layer1_pass,
        "by_evaluator": {
            "helpfulness_v3": {"kind": "judge", "layer": 3,
                               "mean": helpfulness_mean, "pass_rate": 1.0,
                               "n": 5, "fail_count": 0},
        },
        "by_layer": {
            1: {"mean": layer1_pass, "pass_rate": layer1_pass,
                "row_pass_rate": layer1_pass, "n": 15, "rows_evaluated": 5,
                "evaluators": ["json_schema", "length", "not_contains"]},
            3: {"mean": helpfulness_mean, "pass_rate": 1.0,
                "row_pass_rate": 1.0, "n": 5, "rows_evaluated": 5,
                "evaluators": ["helpfulness_v3"]},
        },
        "by_tag": {tag: {"n": 1, "passed": int(rate >= 1.0),
                          "pass_rate": rate}
                   for tag, rate in (tags or {}).items()},
    }


def test_layer_pass_rate_threshold_block_passes() -> None:
    layers = {"heuristics": {"severity": "block", "aggregation": "pass_rate",
                              "threshold": {"min": 1.0}}}
    [g] = evaluate_gates(None, _metrics(layer1_pass=1.0), layers=layers)
    assert g.passed
    assert g.severity == "block"
    assert g.layer == LAYER_INDEX["heuristics"]


def test_layer_pass_rate_threshold_block_fails() -> None:
    layers = {"heuristics": {"severity": "block", "aggregation": "pass_rate",
                              "threshold": {"min": 1.0}}}
    [g] = evaluate_gates(None, _metrics(layer1_pass=0.8), layers=layers)
    assert not g.passed


def test_warn_severity_does_not_block() -> None:
    layers = {"metrics": {"severity": "warn", "aggregation": "pass_rate",
                           "threshold": {"min": 0.9}}}
    [g] = evaluate_gates(None, _metrics(), layers=layers)
    assert g.severity == "warn"
    assert g.blocking is False


def test_per_tag_overrides_drive_pass_fail() -> None:
    layers = {"judge_offline": {
        "severity": "block",
        "aggregation": "pass_rate_by_tag",
        "threshold": {
            "per_tag_overrides": {"safety": 1.0, "helpfulness": 0.8},
        },
    }}
    metrics = _metrics(tags={"safety": 1.0, "helpfulness": 0.85})
    [g] = evaluate_gates(None, metrics, layers=layers)
    assert g.passed
    # Drop safety pass-rate below 1.0 → should fail.
    metrics_bad = _metrics(tags={"safety": 0.99, "helpfulness": 0.85})
    [g_bad] = evaluate_gates(None, metrics_bad, layers=layers)
    assert not g_bad.passed


def test_evaluator_scoped_gate_uses_specific_evaluator() -> None:
    layers = {"judge_offline": {
        "severity": "block", "evaluator": "helpfulness_v3",
        "aggregation": "mean", "threshold": {"min": 4.0},
    }}
    [g] = evaluate_gates(None, _metrics(helpfulness_mean=4.5), layers=layers)
    assert g.passed
    [g_bad] = evaluate_gates(None, _metrics(helpfulness_mean=3.0), layers=layers)
    assert not g_bad.passed


# ---------------------------------------------------------------------------
# run_mode integration tests via the executor


def _project_with_failing_heuristic(base: Path) -> Path:
    (base / "datasets").mkdir()
    (base / "prompts").mkdir()
    # ``mock_provider`` in echo mode echoes the prompt — far over 100 chars,
    # so the length heuristic will fail on every row.
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hello there"}\n'
        '{"id":"r2","input":"hello again"}\n'
    )
    (base / "prompts" / "p.md").write_text(
        "Some longish prompt that will exceed the length limit when echoed "
        "back. " * 5 + "\n{input}"
    )
    cfg = base / "evalguard.yaml"
    cfg.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo } }]\n"
        "prompts: [{ id: p, file: prompts/p.md }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics:\n"
        "  - { type: length, max: 50, unit: chars }\n"
        "judges:\n"
        "  - { id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }\n"
        "layers:\n"
        "  heuristics:    { severity: block, aggregation: pass_rate, threshold: {min: 1.0} }\n"
        "  judge_offline: { severity: block, aggregation: pass_rate, threshold: {min: 0.9} }\n"
        "{run_mode_block}\n"
    )
    return cfg


def _set_run_mode(base: Path, mode: str) -> None:
    p = base / "evalguard.yaml"
    text = p.read_text().replace("{run_mode_block}", f"run_mode: {mode}")
    p.write_text(text)


def test_run_mode_short_circuit_skips_judge(tmp_path: Path) -> None:
    cfg_path = _project_with_failing_heuristic(tmp_path)
    _set_run_mode(tmp_path, "short_circuit_blocking_only")
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    metrics = store.compute_metrics(record.run_id)
    # Layer 3 (judge) should not have been invoked on any row.
    assert 3 not in metrics["by_layer"]
    # Layer 1 ran on every row and failed every row.
    assert metrics["by_layer"][1]["row_pass_rate"] == 0.0


def test_run_mode_always_runs_every_layer(tmp_path: Path) -> None:
    cfg_path = _project_with_failing_heuristic(tmp_path)
    _set_run_mode(tmp_path, "always")
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    metrics = store.compute_metrics(record.run_id)
    # In ``always`` mode the judge runs even though layer 1 failed.
    assert 3 in metrics["by_layer"]
    assert metrics["by_layer"][3]["rows_evaluated"] == record.row_count
