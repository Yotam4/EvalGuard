"""Phase 1a: validate / diff / baseline / relative thresholds."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from evalguard_cli.local.baseline_io import (
    BASELINE_SCHEMA_VERSION,
    load_baseline,
    save_baseline,
)
from evalguard_cli.local.gate import evaluate_gates
from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


_BASELINE_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "packages" / "schemas" / "evalguard.baseline.schema.json"


# ---------------------------------------------------------------------------
# baseline_io


def test_baseline_save_load_roundtrip(tmp_path: Path):
    metrics = {
        "row_count": 5.0,
        "cost_usd": 0.30,
        "pass_rate": 1.0,
        "by_evaluator": {"q": {"kind": "judge", "layer": 3, "mean": 4.5,
                                "pass_rate": 1.0, "n": 5, "fail_count": 0}},
        "by_layer": {3: {"mean": 4.5, "pass_rate": 1.0,
                         "row_pass_rate": 1.0, "rows_evaluated": 5,
                         "n": 5, "evaluators": ["q"]}},
        "by_tag":   {"normal": {"n": 5, "passed": 5, "pass_rate": 1.0}},
    }
    saved = save_baseline(
        run_id="run_abcdef0123456789",
        config_hash="0" * 64,
        metrics=metrics,
        actor_meta={"git_sha": "deadbeef", "git_branch": "main"},
        path=tmp_path / "b.json",
    )
    assert saved.schema_version == BASELINE_SCHEMA_VERSION
    payload = json.loads((tmp_path / "b.json").read_text())
    schema = json.loads(_BASELINE_SCHEMA_PATH.read_text())
    jsonschema.validate(payload, schema)
    loaded = load_baseline(tmp_path / "b.json")
    assert loaded.run_id == saved.run_id
    assert loaded.metrics["pass_rate"] == 1.0
    assert loaded.actor_meta["git_branch"] == "main"


def test_baseline_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_baseline(tmp_path / "missing.json")


def test_baseline_invalid_schema_rejected(tmp_path: Path):
    """A baseline file that doesn't match the schema is refused."""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema_version": "1.0.0"}))   # missing required fields
    with pytest.raises(jsonschema.ValidationError):
        load_baseline(p)


# ---------------------------------------------------------------------------
# relative thresholds


def _metrics(*, pass_rate: float = 1.0, judge_mean: float = 4.5,
             cost_usd: float = 0.30) -> dict:
    return {
        "row_count": 5.0,
        "cost_usd": cost_usd,
        "pass_rate": pass_rate,
        "by_evaluator": {"q": {"kind": "judge", "layer": 3,
                                "mean": judge_mean, "pass_rate": pass_rate,
                                "n": 5, "fail_count": 0}},
        "by_layer": {3: {"mean": judge_mean, "pass_rate": pass_rate,
                         "row_pass_rate": pass_rate,
                         "rows_evaluated": 5, "n": 5,
                         "evaluators": ["q"]}},
        "by_tag":   {},
    }


def test_relative_threshold_passes_when_delta_within_bounds():
    layers = {"judge_offline": {
        "severity": "block",
        "aggregation": "pass_rate",
        "threshold": {
            "type": "relative",
            "min_delta_vs_baseline": -0.02,
        },
    }}
    base = _metrics(pass_rate=1.0)
    curr = _metrics(pass_rate=0.99)   # only 0.01 worse, within bounds
    [g] = evaluate_gates(None, curr, layers=layers, baseline=base)
    assert g.passed
    delta_detail = next(d for d in g.details
                        if d["metric"].endswith("delta_vs_baseline"))
    assert delta_detail["actual"] == pytest.approx(-0.01)
    assert delta_detail["baseline"] == pytest.approx(1.0)


def test_relative_threshold_fails_when_delta_exceeds_bounds():
    layers = {"judge_offline": {
        "severity": "block",
        "aggregation": "pass_rate",
        "threshold": {
            "type": "relative",
            "min_delta_vs_baseline": -0.02,
        },
    }}
    base = _metrics(pass_rate=1.0)
    curr = _metrics(pass_rate=0.95)   # 0.05 regression, breaches -0.02 floor
    [g] = evaluate_gates(None, curr, layers=layers, baseline=base)
    assert not g.passed


def test_relative_threshold_skipped_when_no_baseline():
    """Same YAML must pass when run without a baseline (relative rules
    are no-ops in that case)."""
    layers = {"judge_offline": {
        "severity": "block",
        "aggregation": "pass_rate",
        "threshold": {
            "type": "relative",
            "min_delta_vs_baseline": -0.02,
        },
    }}
    [g] = evaluate_gates(None, _metrics(pass_rate=0.5), layers=layers)
    # No min/max absolute and no baseline → no rules to fail.
    assert g.passed


def test_relative_max_delta_used_for_lower_better_metrics():
    """Cost / latency style metrics use ``max_delta_vs_baseline`` to
    cap upward drift."""
    layers = {"judge_offline": {
        "severity": "block",
        "aggregation": "mean",
        "evaluator": "q",
        "threshold": {
            "type": "relative",
            "max_delta_vs_baseline": 0.10,
        },
    }}
    base = _metrics(judge_mean=4.5)
    curr_ok = _metrics(judge_mean=4.55)    # +0.05 ≤ 0.10 → passes
    curr_bad = _metrics(judge_mean=4.70)   # +0.20 > 0.10 → fails
    [g_ok] = evaluate_gates(None, curr_ok, layers=layers, baseline=base)
    [g_bad] = evaluate_gates(None, curr_bad, layers=layers, baseline=base)
    assert g_ok.passed
    assert not g_bad.passed


# ---------------------------------------------------------------------------
# validate command (smoke)


def _write_min_project(base: Path) -> Path:
    (base / "datasets").mkdir(parents=True, exist_ok=True)
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi"}\n'
    )
    cfg = base / "evalguard.yaml"
    cfg.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
    )
    return cfg


def test_validate_accepts_a_valid_config(tmp_path: Path):
    cfg = _write_min_project(tmp_path)
    # Loading succeeds → validate would exit 0.
    loaded = load_config(cfg)
    assert loaded.project == "t"
    assert loaded.assets   # at least the dataset


def test_validate_rejects_invalid_config(tmp_path: Path):
    """jsonschema validation catches a missing required field."""
    cfg = tmp_path / "evalguard.yaml"
    cfg.write_text("version: 1\nproject: t\n")   # no providers / datasets
    with pytest.raises(Exception):
        load_config(cfg)


def test_validate_cli_smoke(tmp_path: Path):
    """End-to-end CLI: ``evalguard validate`` exits 0 on a good config
    and 1 on a bad one. Uses subprocess so the Typer wiring is real."""
    cfg = _write_min_project(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "evalguard_cli.main", "validate", "-c", str(cfg), "-q"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr

    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nproject: t\n")
    result_bad = subprocess.run(
        [sys.executable, "-m", "evalguard_cli.main", "validate", "-c", str(bad), "-q"],
        capture_output=True, text=True, timeout=10,
    )
    assert result_bad.returncode == 1


# ---------------------------------------------------------------------------
# end-to-end: --save-baseline then --baseline drives a relative gate


def test_save_baseline_then_relative_gate_passes(tmp_path: Path):
    """Round-trip: run, save baseline, run again with same config that
    produces same scores → relative gate passes (Δ = 0)."""
    cfg_path = tmp_path / "evalguard.yaml"
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi"}\n'
        '{"id":"r2","input":"yo"}\n'
    )
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "judges:\n  - { id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }\n"
        "layers:\n"
        "  judge_offline:\n"
        "    severity: block\n"
        "    aggregation: pass_rate\n"
        "    threshold:\n"
        "      type: relative\n"
        "      min_delta_vs_baseline: -0.02\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    metrics = store.compute_metrics(record.run_id)

    baseline_path = tmp_path / "baseline.json"
    save_baseline(
        run_id=record.run_id,
        config_hash=cfg.config_hash,
        metrics=metrics,
        path=baseline_path,
    )
    base = load_baseline(baseline_path).metrics

    # Re-evaluate gates with the baseline — should pass since metrics match.
    layers = cfg.raw["layers"]
    [g_pass] = evaluate_gates(None, metrics, layers=layers, baseline=base)
    assert g_pass.passed

    # Now simulate a regression: same metrics shape but lower pass_rate.
    regressed = dict(metrics)
    regressed["by_layer"] = {3: {**metrics["by_layer"][3], "pass_rate": 0.5}}
    [g_fail] = evaluate_gates(None, regressed, layers=layers, baseline=base)
    assert not g_fail.passed


# ---------------------------------------------------------------------------
# diff helper math


def test_diff_classifies_regress_vs_improve():
    from evalguard_cli.commands.diff_cmd import _build_diff_rows
    a = {"pass_rate": 1.0, "cost_usd": 0.5,
         "by_evaluator": {}, "by_layer": {}, "by_tag": {}}
    b = {"pass_rate": 0.9, "cost_usd": 0.3,
         "by_evaluator": {}, "by_layer": {}, "by_tag": {}}
    rows = _build_diff_rows(a, b)
    by_metric = {r[0]: r for r in rows}
    # pass_rate dropped → regress
    assert by_metric["pass_rate"][4] == "regress"
    # cost_usd dropped (lower is better) → improve
    assert by_metric["cost_usd"][4] == "improve"
