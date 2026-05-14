"""Phase 1.5 — entrypoint.sh behaviour with mocked CLI.

These tests treat ``packages/action/entrypoint.sh`` as a black box.
The fixture wires a ``$PATH``-shadowing fake ``evalguard`` script
that emits canned output for each subcommand, then runs the
entrypoint and asserts on:

- ``$GITHUB_OUTPUT`` lines (the action's typed outputs)
- ``$GITHUB_STEP_SUMMARY`` markdown (the workflow-UI surface)
- the script's exit code (gating)

Why not run the real CLI?  The action wraps it but the wrapping
*is* the surface we're testing — token redaction, output parsing,
``fail-on`` gating, push-vs-no-push branching.  Mocking the CLI
lets us exercise every branch without spending an LLM call per
test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ENTRYPOINT = Path(__file__).resolve().parents[1] / "packages" / "action" / "entrypoint.sh"


def _write_fake_evalguard(
    bin_dir: Path,
    *,
    view_json: str = '{"gate_status":"passed","cost_usd":0.0042}',
    push_exit: int = 0,
    run_exit:  int = 0,
    run_writes_db: bool = True,
) -> None:
    """Drop a fake ``evalguard`` script into ``bin_dir``.

    The script dispatches on ``$1`` (the subcommand) so each branch
    can return a different canned response.  ``run`` optionally
    materialises ``.evalguard/local.db`` so the entrypoint's
    ``RUN_ID`` SELECT succeeds.
    """
    fake = bin_dir / "evalguard"
    fake.write_text(f"""#!/usr/bin/env bash
case "$1" in
  validate) exit 0 ;;
  run)
    if [ "{int(run_writes_db)}" = "1" ]; then
      mkdir -p .evalguard
      python3 -c '
import sqlite3
c = sqlite3.connect(".evalguard/local.db")
c.execute("CREATE TABLE IF NOT EXISTS runs(run_id TEXT, started_at TEXT)")
c.execute("INSERT INTO runs(run_id, started_at) VALUES (?, ?)",
          ("run_faked0000aaaa", "2026-05-14T00:00:00"))
c.commit()
'
    fi
    exit {run_exit} ;;
  push)
    exit {push_exit} ;;
  view)
    # Emit only when ``--json`` is present, matching the real CLI.
    for arg in "$@"; do [ "$arg" = "--json" ] && {{
      cat <<'JSON'
{view_json}
JSON
      exit 0
    }}; done
    exit 0 ;;
  comment) exit 0 ;;
  --version) echo "evalguard 0.0.0-fake" ;;
  *) exit 1 ;;
esac
""")
    fake.chmod(0o755)


def _run_entrypoint(
    cwd: Path,
    bin_dir: Path,
    env_overrides: dict[str, str],
) -> tuple[int, dict[str, str], str]:
    """Run entrypoint.sh with the fake CLI on PATH.  Returns
    ``(exit_code, GITHUB_OUTPUT dict, GITHUB_STEP_SUMMARY text)``."""
    gh_out  = cwd / "github_output"
    gh_sum  = cwd / "github_summary"
    gh_out.write_text("")
    gh_sum.write_text("")
    # Inherit the parent env so HOME / LANG / runtime python paths
    # survive into the subprocess, then layer the action's inputs on
    # top.  Wiping the env entirely (env={...}) broke ``python3``
    # subshells inside the entrypoint.
    env = {
        **os.environ,
        # Front-load the fake evalguard onto PATH.
        "PATH":                 f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_OUTPUT":        str(gh_out),
        "GITHUB_STEP_SUMMARY":  str(gh_sum),
        "GITHUB_WORKSPACE":     str(cwd),
        # Don't inherit GITHUB_EVENT_PATH from a parent test runner —
        # the entrypoint's PR-number auto-detect would try to ``jq``
        # against an unrelated event file and pollute the output.
        "GITHUB_EVENT_PATH":    "",
        # Empty by default — individual tests override.
        "EVALGUARD_INPUT_CONFIG":  "evalguard.yaml",
        "EVALGUARD_INPUT_BASELINE": "",
        "EVALGUARD_INPUT_SAVE_BASELINE": "",
        "EVALGUARD_INPUT_COMMENT": "false",  # don't touch GH API
        "EVALGUARD_INPUT_MARKER":  "<!-- m -->",
        "EVALGUARD_INPUT_PR_NUMBER": "",
        "EVALGUARD_INPUT_FAIL_FAST": "false",
        "EVALGUARD_INPUT_SERVER":   "",
        "EVALGUARD_INPUT_TOKEN":    "",
        "EVALGUARD_INPUT_PUSH":     "false",
        "EVALGUARD_INPUT_FAIL_ON":  "gate_failed",
        **env_overrides,
    }
    # A stub config file the validate step will accept (the fake
    # ``evalguard validate`` is a no-op so contents don't matter).
    (cwd / "evalguard.yaml").write_text("version: 1\n")
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )
    outputs: dict[str, str] = {}
    for line in gh_out.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            outputs[k] = v
    return proc.returncode, outputs, gh_sum.read_text()


# ---------------------------------------------------------------------------
# Output emission


def test_entrypoint_emits_every_v1_output_on_happy_path(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _write_fake_evalguard(bin_dir)
    code, outs, summary = _run_entrypoint(tmp_path, bin_dir, {})
    assert code == 0
    # Every output the action.yml declares is actually written.
    for k in ("exit_code", "run_id", "gate_status", "cost_usd", "url"):
        assert k in outs, f"entrypoint did not write {k}"
    assert outs["run_id"]      == "run_faked0000aaaa"
    assert outs["gate_status"] == "passed"
    assert outs["cost_usd"]    == "0.0042"
    # ``url`` is empty when there's no server configured.
    assert outs["url"] == ""
    # Step summary mentions the run.
    assert "run_faked0000aaaa" in summary
    assert "passed" in summary


def test_entrypoint_writes_url_when_server_and_token_are_set(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _write_fake_evalguard(bin_dir)
    code, outs, summary = _run_entrypoint(tmp_path, bin_dir, {
        "EVALGUARD_INPUT_PUSH":   "true",
        "EVALGUARD_INPUT_SERVER": "https://eval.example.com",
        "EVALGUARD_INPUT_TOKEN":  "evk_secret",
    })
    assert code == 0
    # URL is computed client-side: ``${server}/v1/runs/${run_id}``.
    # Don't depend on the CLI's stdout for this — its format isn't
    # a stability contract.
    assert outs["url"] == "https://eval.example.com/v1/runs/run_faked0000aaaa"
    assert "https://eval.example.com/v1/runs/run_faked0000aaaa" in summary


def test_entrypoint_strips_trailing_slash_on_server(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _write_fake_evalguard(bin_dir)
    code, outs, _ = _run_entrypoint(tmp_path, bin_dir, {
        "EVALGUARD_INPUT_PUSH":   "true",
        "EVALGUARD_INPUT_SERVER": "https://eval.example.com/",
        "EVALGUARD_INPUT_TOKEN":  "evk_secret",
    })
    assert code == 0
    # No double-slash in the URL.
    assert outs["url"] == "https://eval.example.com/v1/runs/run_faked0000aaaa"


def test_entrypoint_skips_push_without_token_or_server(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    # Make ``push`` exit non-zero so a wrongful invocation would
    # surface in the warning channel.
    _write_fake_evalguard(bin_dir, push_exit=99)

    # Server set, token empty — must not push.
    code, outs, _ = _run_entrypoint(tmp_path, bin_dir, {
        "EVALGUARD_INPUT_PUSH":   "true",
        "EVALGUARD_INPUT_SERVER": "https://eval.example.com",
        "EVALGUARD_INPUT_TOKEN":  "",
    })
    assert code == 0
    assert outs["url"] == ""

    # ``push: false`` even with both secrets — must not push.
    code, outs, _ = _run_entrypoint(tmp_path, bin_dir, {
        "EVALGUARD_INPUT_PUSH":   "false",
        "EVALGUARD_INPUT_SERVER": "https://eval.example.com",
        "EVALGUARD_INPUT_TOKEN":  "evk_secret",
    })
    assert code == 0
    assert outs["url"] == ""


# ---------------------------------------------------------------------------
# fail-on gating


@pytest.mark.parametrize(
    "fail_on, gate_status, expected_exit",
    [
        # Default behaviour: only gate_failed fails.  Exit 2 (not 1)
        # so CI consumers can distinguish "gate failed" (2) from
        # "infra error" (1) — preserved from the v0 action.
        ("gate_failed", "passed",     0),
        ("gate_failed", "warned",     0),
        ("gate_failed", "row_failed", 0),
        ("gate_failed", "gate_failed", 2),
        # Widened: warned also fails.
        ("gate_failed,warned", "warned",     2),
        ("gate_failed,warned", "passed",     0),
        # Whitespace tolerance — operators wrap lists across lines.
        ("gate_failed, warned", "warned",    2),
        # ``never`` short-circuits everything.
        ("never", "gate_failed", 0),
        ("never", "warned",      0),
    ],
)
def test_fail_on_filters_gate_status(tmp_path, fail_on, gate_status, expected_exit):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _write_fake_evalguard(
        bin_dir,
        view_json=f'{{"gate_status":"{gate_status}","cost_usd":0.0}}',
        # ``run`` exits 2 on real gate-fail, but we set 0 here so the
        # test isolates ``fail-on`` behaviour from RUN_EXIT propagation.
        run_exit=0,
    )
    code, _, _ = _run_entrypoint(tmp_path, bin_dir, {
        "EVALGUARD_INPUT_FAIL_ON": fail_on,
    })
    assert code == expected_exit, (fail_on, gate_status)


def test_infra_error_always_fails_regardless_of_fail_on(tmp_path):
    """RUN_EXIT non-zero AND no gate_status (i.e., no JSON parsed) is
    an infra error that fail-on must not silence — including
    ``fail-on: never``."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _write_fake_evalguard(
        bin_dir,
        run_exit=1,
        run_writes_db=False,   # no run row ⇒ no JSON
        view_json="",
    )
    for fail_on in ("never", "gate_failed", "warned"):
        code, _, _ = _run_entrypoint(tmp_path, bin_dir, {
            "EVALGUARD_INPUT_FAIL_ON": fail_on,
        })
        assert code != 0, f"infra error swallowed under fail_on={fail_on}"


# ---------------------------------------------------------------------------
# Step summary


def test_step_summary_falls_back_when_run_did_not_complete(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _write_fake_evalguard(bin_dir, run_exit=1, run_writes_db=False, view_json="")
    code, outs, summary = _run_entrypoint(tmp_path, bin_dir, {
        "EVALGUARD_INPUT_FAIL_ON": "never",  # don't fail; we want to read summary
    })
    # ``never`` doesn't silence infra error, so code is non-zero.
    assert code != 0
    assert "did not produce" in summary


# ---------------------------------------------------------------------------
# Secret handling


def test_token_does_not_appear_in_outputs_or_summary_on_push_failure(tmp_path):
    """A push that exits non-zero must not surface the token via
    GITHUB_OUTPUT, step summary, or stdout/stderr.  GitHub auto-
    masks ``${{ secrets.* }}`` in workflow logs, but the OUTPUT
    file is consumed by downstream steps directly and a leak here
    would be hard to spot."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _write_fake_evalguard(bin_dir, push_exit=42)  # fail the push
    secret = "evk_supersecret_token_must_not_leak"
    code, outs, summary = _run_entrypoint(tmp_path, bin_dir, {
        "EVALGUARD_INPUT_PUSH":   "true",
        "EVALGUARD_INPUT_SERVER": "https://eval.example.com",
        "EVALGUARD_INPUT_TOKEN":  secret,
    })
    # ``url`` should be empty (push failed); CI keeps going.
    assert outs.get("url", "") == ""
    # Neither the outputs file nor the summary should carry the
    # token in any form.
    for k, v in outs.items():
        assert secret not in v, f"token leaked into output {k}={v}"
    assert secret not in summary
