"""Phase 1b: PR-comment markdown renderer + Action shape sanity."""

from __future__ import annotations

from pathlib import Path

import yaml

from evalguard_cli.commands.comment_cmd import (
    DEFAULT_MARKER,
    render_comment,
)


# ---------------------------------------------------------------------------
# Renderer (pure-function golden checks)


def _payload(*, status: str = "passed", trials: int = 1, with_gates: bool = True) -> dict:
    """Synthesise a minimal run payload mimicking ``serializer.run_to_dict``."""
    trial_objs = []
    for i in range(trials):
        trial_objs.append({
            "trial_id":      f"trial_{i:08x}deadbeef",
            "provider_id":   f"mock:m{i}",
            "provider":      "mock",
            "model":         f"m{i}",
            "row_count":     5,
            "row_pass_count": 5,
            "row_fail_count": 0,
            "cost_usd":      0.0,
            "status":        "passed",
            "gate_status":   "passed",
            "gates": [
                {"gate_name": "judge_offline", "severity": "block",
                 "passed": status != "gate_failed", "details": []}
            ] if with_gates else [],
            "metrics":       {},
        })
    return {
        "schema_version": "1.0.0",
        "run_id": "run_" + "a" * 16,
        "project": "demo",
        "config_hash": "f" * 64,
        "status": status,
        "row_count": 5 * trials,
        "row_pass_count": 5 * trials,
        "row_fail_count": 0,
        "cost_usd": 0.0,
        "trials": trial_objs,
        "comparison": {"best_by": {}, "trials": []},
        "aggregate": {
            "metrics": {
                "pass_rate": 1.0,
                "cost_usd": 0.0,
                "by_evaluator": {
                    "helpfulness_v3": {"mean": 4.5, "pass_rate": 1.0},
                },
            },
            "gates": [],
        },
    }


def test_render_starts_with_marker():
    body = render_comment(_payload())
    assert body.startswith(DEFAULT_MARKER)


def test_render_uses_custom_marker():
    body = render_comment(_payload(), marker="<!-- ci:eval -->")
    assert body.startswith("<!-- ci:eval -->")


def test_header_reflects_status():
    assert "✅ passed" in render_comment(_payload(status="passed"))
    assert "❌ gate_failed" in render_comment(_payload(status="gate_failed"))
    assert "⚠️ warned" in render_comment(_payload(status="warned"))


def test_summary_counts_passed_rows_and_trials():
    body = render_comment(_payload(trials=3))
    assert "**15/15** row-evaluations passed" in body
    assert "3 trials" in body


def test_trials_table_rendered_for_multi_trial_runs():
    body = render_comment(_payload(trials=2))
    assert "### Trials" in body
    assert "| Trial | Provider | Status | Gate | Rows | Cost |" in body


def test_trials_table_omitted_for_single_trial():
    body = render_comment(_payload(trials=1))
    assert "### Trials" not in body


def test_gates_section_lists_per_trial_verdicts():
    body = render_comment(_payload(trials=2))
    assert "### Gates" in body
    # One row per (trial × gate). 2 trials × 1 gate = 2 data rows.
    gate_lines = [l for l in body.splitlines()
                  if l.startswith("| `judge_offline`")]
    assert len(gate_lines) == 2


def test_delta_table_renders_when_baseline_provided():
    payload = _payload()
    baseline = {
        "pass_rate": 1.0,
        "cost_usd": 0.0,
        "by_evaluator": {
            "helpfulness_v3": {"mean": 5.0, "pass_rate": 1.0},
        },
    }
    body = render_comment(payload, baseline=baseline, baseline_run_id="run_baseline" + "0"*8)
    assert "### Δ vs baseline" in body
    # helpfulness_v3.mean dropped 5.0 → 4.5, regression marker present.
    assert "helpfulness_v3.mean" in body
    assert "-0.5000" in body


def test_delta_table_omitted_when_no_baseline():
    body = render_comment(_payload())
    assert "Δ vs baseline" not in body


def test_provenance_line_present():
    body = render_comment(_payload())
    assert "run_id:" in body
    assert "config_hash:" in body
    assert "schema:" in body


# ---------------------------------------------------------------------------
# action.yml sanity


def test_action_yml_is_valid_and_declares_required_pieces():
    """Catch syntax errors / missing inputs early — the Action runs in
    Docker on user repos, so a broken action.yml fails everyone's CI."""
    p = Path(__file__).resolve().parents[1] / "packages" / "action" / "action.yml"
    data = yaml.safe_load(p.read_text())
    assert data["name"] == "EvalGuard"
    inputs = data["inputs"]
    # v0 inputs.
    for required in ("config", "baseline", "save_baseline", "comment", "marker", "github_token"):
        assert required in inputs, f"action.yml missing input: {required}"
    # v1 inputs (Phase 1.5).
    for required in ("server", "token", "push", "fail_on"):
        assert required in inputs, f"action.yml missing v1 input: {required}"
    outputs = data["outputs"]
    # v0 outputs.
    for required in ("exit_code", "run_id", "comment_url"):
        assert required in outputs, f"action.yml missing output: {required}"
    # v1 outputs (Phase 1.5).
    for required in ("gate_status", "cost_usd", "url"):
        assert required in outputs, f"action.yml missing v1 output: {required}"
    # Every output needs a ``description`` so the GitHub UI renders
    # it.  (For composite actions ``value`` is also required; this is
    # a Docker action so the entrypoint writes ``$GITHUB_OUTPUT``
    # directly — there's a sibling test below that asserts the
    # entrypoint actually emits every declared output.)
    for k in ("run_id", "exit_code", "comment_url",
              "gate_status", "cost_usd", "url"):
        assert "description" in outputs[k], f"output {k} missing description"
    assert data["runs"]["using"] == "docker"
    assert data["runs"]["image"] == "Dockerfile"
    # The v1 input env-block in ``runs.env`` must forward every v1
    # input into ``EVALGUARD_INPUT_*`` so the entrypoint can read it.
    env = data["runs"]["env"]
    for var, key in (
        ("EVALGUARD_INPUT_SERVER",  "inputs.server"),
        ("EVALGUARD_INPUT_TOKEN",   "inputs.token"),
        ("EVALGUARD_INPUT_PUSH",    "inputs.push"),
        ("EVALGUARD_INPUT_FAIL_ON", "inputs.fail_on"),
    ):
        assert var in env, f"runs.env missing {var}"
        assert key in env[var], f"runs.env[{var}] not wired to {key}"
