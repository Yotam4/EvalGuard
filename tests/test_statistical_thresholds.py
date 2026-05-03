"""Statistical-threshold gates — Welch's t-test on per-evaluator score
samples between a baseline run and the current run.

Three layers:

1. Pure-Python Welch's t-test correctness — pinned against hand-computed
   values for tiny inputs and against scipy-equivalent expectations for
   the qualitative cases (clear difference / no difference).
2. Baseline-IO carries ``samples`` through the schema (1.1.0).
3. End-to-end through ``evaluate_gates``: a stable run vs a regressed
   run produces a failing ttest gate; vs a same-distribution run passes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import jsonschema

from evalguard_cli.local.baseline_io import (
    BASELINE_SCHEMA_VERSION, load_baseline, save_baseline,
)
from evalguard_cli.local.gate import evaluate_gates
from evalguard_cli.local.stats import welchs_t_test


_BASELINE_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "packages" / "schemas" / "evalguard.baseline.schema.json"


# ---------------------------------------------------------------------------
# 1. Welch's t-test correctness


def test_welch_identical_samples_p_one():
    """Two identical samples → t=0 → p=1 (no detectable difference)."""
    r = welchs_t_test([1.0, 2.0, 3.0, 4.0, 5.0],
                      [1.0, 2.0, 3.0, 4.0, 5.0])
    assert r.t_stat == 0.0
    assert math.isclose(r.p_two_sided, 1.0, abs_tol=1e-9)
    assert math.isclose(r.p_less, 0.5, abs_tol=1e-9)
    assert math.isclose(r.p_greater, 0.5, abs_tol=1e-9)


def test_welch_clearly_lower_current_gives_small_p_less():
    """Current much lower than baseline → very small p_less."""
    current  = [1.0] * 20 + [1.5] * 5    # mean ≈ 1.1
    baseline = [4.0] * 20 + [4.5] * 5    # mean ≈ 4.1
    r = welchs_t_test(current, baseline)
    assert r.t_stat < 0
    assert r.p_less < 0.01           # massively lower → significant
    assert r.p_greater > 0.99
    assert r.p_two_sided < 0.02


def test_welch_clearly_higher_current_gives_small_p_greater():
    current  = [4.0] * 20 + [4.5] * 5
    baseline = [1.0] * 20 + [1.5] * 5
    r = welchs_t_test(current, baseline)
    assert r.t_stat > 0
    assert r.p_greater < 0.01
    assert r.p_less > 0.99


def test_welch_overlapping_distributions_no_significance():
    """Drawn from same distribution → p_two_sided not significant."""
    current  = [4.0, 4.5, 4.2, 4.7, 4.1, 4.8, 4.3, 4.6, 4.4, 4.5]
    baseline = [4.5, 4.2, 4.6, 4.3, 4.4, 4.7, 4.1, 4.5, 4.6, 4.4]
    r = welchs_t_test(current, baseline)
    # Means within ~0.05; should be far from significant at alpha=0.05.
    assert r.p_two_sided > 0.10


def test_welch_known_value_against_reference():
    """Against scipy.stats.ttest_ind(equal_var=False) reference values:

        a = list(range(1, 11))      # 1..10
        b = list(range(3, 13))      # 3..12
        scipy → t ≈ -1.4771, dof = 18.0, p_two_sided ≈ 0.15693

    Matches our pure-Python CF-based incomplete-beta to ~5 decimals."""
    a = list(range(1, 11))
    b = list(range(3, 13))
    r = welchs_t_test(a, b)
    assert math.isclose(r.t_stat, -1.4771, rel_tol=0.001)
    assert math.isclose(r.dof, 18.0, abs_tol=1e-9)
    assert math.isclose(r.p_two_sided, 0.15693, rel_tol=0.01)


def test_welch_raises_on_too_few_samples():
    import pytest
    with pytest.raises(ValueError, match="≥2 samples"):
        welchs_t_test([1.0], [1.0, 2.0])


# ---------------------------------------------------------------------------
# 2. Baseline schema 1.1.0 round-trip with samples


def test_baseline_carries_samples_under_1_1_0(tmp_path: Path):
    metrics = {
        "row_count": 10.0,
        "cost_usd":  0.0,
        "by_evaluator": {
            "judge": {"kind": "judge", "layer": 3, "mean": 4.4,
                      "pass_rate": 1.0, "n": 10, "fail_count": 0},
        },
        "by_layer": {3: {"mean": 4.4, "pass_rate": 1.0,
                         "row_pass_rate": 1.0, "rows_evaluated": 10,
                         "n": 10, "evaluators": ["judge"]}},
        "by_tag": {},
        "samples": {
            "judge": [4.5, 4.4, 4.5, 4.3, 4.4, 4.5, 4.4, 4.3, 4.4, 4.5],
        },
    }
    saved = save_baseline(
        run_id="run_abcdef0123456789",
        config_hash="0" * 64,
        metrics=metrics,
        actor_meta={},
        path=tmp_path / "b.json",
    )
    assert saved.schema_version == BASELINE_SCHEMA_VERSION == "1.1.0"
    payload = json.loads((tmp_path / "b.json").read_text())
    schema = json.loads(_BASELINE_SCHEMA_PATH.read_text())
    jsonschema.validate(payload, schema)
    loaded = load_baseline(tmp_path / "b.json")
    assert loaded.metrics["samples"]["judge"] == metrics["samples"]["judge"]


# ---------------------------------------------------------------------------
# 3. End-to-end gate evaluation


def _metrics(judge_samples: list[float]) -> dict:
    return {
        "row_count": float(len(judge_samples)),
        "cost_usd":  0.0,
        "by_evaluator": {
            "judge": {"kind": "judge", "layer": 3,
                      "mean": sum(judge_samples) / len(judge_samples),
                      "pass_rate": 1.0, "n": len(judge_samples), "fail_count": 0},
        },
        "by_layer": {3: {"mean": sum(judge_samples) / len(judge_samples),
                         "pass_rate": 1.0, "row_pass_rate": 1.0,
                         "rows_evaluated": len(judge_samples),
                         "n": len(judge_samples), "evaluators": ["judge"]}},
        "by_tag": {},
        "samples": {"judge": list(judge_samples)},
    }


def test_ttest_gate_passes_when_distributions_overlap():
    layers = {"judge_offline": {
        "severity": "block",
        "evaluator": "judge",
        "threshold": {"type": "ttest", "alpha": 0.05, "alternative": "less"},
    }}
    base = _metrics([4.5, 4.4, 4.6, 4.3, 4.5, 4.4, 4.6, 4.5, 4.4, 4.5])
    cur  = _metrics([4.4, 4.5, 4.5, 4.6, 4.4, 4.5, 4.4, 4.6, 4.5, 4.4])
    [g] = evaluate_gates(None, cur, layers=layers, baseline=base)
    assert g.passed
    detail = next(d for d in g.details if d["metric"] == "ttest.p_value")
    assert detail["alternative"] == "less"
    assert detail["passed"] is True


def test_ttest_gate_fails_on_significant_regression():
    layers = {"judge_offline": {
        "severity": "block",
        "evaluator": "judge",
        "threshold": {"type": "ttest", "alpha": 0.05, "alternative": "less"},
    }}
    base = _metrics([4.5] * 20 + [4.6] * 10)   # mean ≈ 4.53
    cur  = _metrics([3.5] * 20 + [3.6] * 10)   # mean ≈ 3.53 — clear regression
    [g] = evaluate_gates(None, cur, layers=layers, baseline=base)
    assert not g.passed
    detail = next(d for d in g.details if d["metric"] == "ttest.p_value")
    assert detail["actual"] < 0.05
    assert detail["t_stat"] < 0
    assert detail["delta_mean"] < 0


def test_ttest_gate_skips_non_blockingly_with_too_few_samples():
    layers = {"judge_offline": {
        "severity": "block",
        "evaluator": "judge",
        "threshold": {"type": "ttest", "alpha": 0.05, "min_n": 10},
    }}
    base = _metrics([4.5, 4.5, 4.5])
    cur  = _metrics([4.5, 4.5, 4.5])
    [g] = evaluate_gates(None, cur, layers=layers, baseline=base)
    assert g.passed   # skipped, not failed
    detail = next(d for d in g.details if d["op"] == "skip")
    assert "insufficient samples" in detail["reason"]


def test_ttest_gate_skips_non_blockingly_when_baseline_lacks_samples():
    """A 1.0.0 baseline (no ``samples`` key) → gate skips non-blockingly."""
    layers = {"judge_offline": {
        "severity": "block",
        "evaluator": "judge",
        "threshold": {"type": "ttest", "alpha": 0.05},
    }}
    base = {"row_count": 10, "cost_usd": 0, "by_evaluator": {}, "by_layer": {}, "by_tag": {}}
    cur  = _metrics([4.5] * 10)
    [g] = evaluate_gates(None, cur, layers=layers, baseline=base)
    assert g.passed
    detail = next(d for d in g.details if d["op"] == "skip")
    assert "insufficient samples" in detail["reason"]


def test_ttest_gate_requires_evaluator_scope():
    """Without ``evaluator: <id>`` the gate has no distribution to test
    and must fail loudly so users notice the misconfiguration."""
    layers = {"judge_offline": {
        "severity": "block",
        "threshold": {"type": "ttest", "alpha": 0.05},
    }}
    cur  = _metrics([4.5] * 10)
    base = _metrics([4.5] * 10)
    [g] = evaluate_gates(None, cur, layers=layers, baseline=base)
    assert not g.passed
    detail = next(d for d in g.details if d["op"] == "config")
    assert "requires 'evaluator: <id>'" in detail["error"]


def test_ttest_alternative_greater_fails_on_cost_regression():
    """``alternative: greater`` is the cost/latency variant — fails when
    the current side is significantly *higher* than baseline. We pin
    to ``judge_offline`` (a registered LAYER_INDEX key) and reuse the
    ``judge`` evaluator id as a stand-in for any per-row numeric metric."""
    layers = {"judge_offline": {
        "severity": "warn",
        "evaluator": "judge",
        "threshold": {"type": "ttest", "alpha": 0.05, "alternative": "greater"},
    }}
    base = _metrics([0.10] * 30)
    cur  = _metrics([0.50] * 30)   # much higher
    [g] = evaluate_gates(None, cur, layers=layers, baseline=base)
    assert not g.passed
    detail = next(d for d in g.details if d["metric"] == "ttest.p_value")
    assert detail["alternative"] == "greater"
    assert detail["delta_mean"] > 0


def test_ttest_gate_fails_loudly_on_unknown_evaluator():
    """A typo'd ``evaluator: <id>`` must be a hard failure, not a
    silent skip — silent skips hide config bugs in CI."""
    layers = {"judge_offline": {
        "severity": "block",
        "evaluator": "judge_typo",   # not present in metrics.by_evaluator
        "threshold": {"type": "ttest", "alpha": 0.05},
    }}
    cur  = _metrics([4.5] * 10)    # samples for "judge", NOT "judge_typo"
    base = _metrics([4.5] * 10)
    [g] = evaluate_gates(None, cur, layers=layers, baseline=base)
    assert not g.passed
    detail = next(d for d in g.details if d["op"] == "config")
    assert "unknown evaluator 'judge_typo'" in detail["error"]
