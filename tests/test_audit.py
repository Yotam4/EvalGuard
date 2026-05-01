"""Audit log: emission, hash chain integrity, tamper detection, redaction."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3

import jsonschema

from evalguard_cli.local.actor import resolve_actor
from evalguard_cli.local.audit import verify_chain
from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


_RUN_SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[1] / "packages" / "schemas" / "evalguard.run.schema.json"


# ---------------------------------------------------------------------------
# fixtures


def _project(base, *, providers=("mock:m1",), redact: bool = False) -> pathlib.Path:
    (base / "datasets").mkdir(parents=True, exist_ok=True)
    (base / "prompts").mkdir(parents=True, exist_ok=True)
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi","tags":["normal"]}\n'
        '{"id":"r2","input":"yo","tags":["edge"]}\n'
    )
    (base / "prompts" / "p.md").write_text("Echo: {input}")
    cfg = base / "evalguard.yaml"
    provider_block = "\n".join(
        f"  - {{ id: '{p}', config: {{ mode: echo }} }}"
        for p in providers
    )
    audit_block = (
        "audit:\n  redact_payload: true\n" if redact else ""
    )
    cfg.write_text(
        "version: 1\nproject: t\n"
        "providers:\n" + provider_block + "\n"
        "prompts: [{ id: p, file: prompts/p.md }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "judges:\n  - { id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }\n"
        "layers:\n"
        "  judge_offline: { severity: block, aggregation: pass_rate, threshold: {min: 0.9} }\n"
        + audit_block
    )
    return cfg


def _run(tmp_path, **kwargs):
    cfg = load_config(_project(tmp_path, **kwargs))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return store, record


# ---------------------------------------------------------------------------
# emission


def test_run_emits_full_event_lifecycle(tmp_path):
    store, record = _run(tmp_path, providers=("mock:m1", "mock:m2"))
    events = store.list_events(record.run_id)

    kinds = [e["kind"] for e in events]
    # Bookends + per-trial + per-row + per-evaluator + per-gate.
    assert kinds[0] == "run.started"
    # Two trials → two trial.started + two trial.finalized
    assert kinds.count("trial.started") == 2
    assert kinds.count("trial.finalized") == 2
    # 2 rows × 2 trials = 4 provider calls
    assert kinds.count("provider.called") == 4
    # 1 judge × 2 rows × 2 trials = 4 judge invocations
    assert kinds.count("evaluator.judge.invoked") == 4


def test_judge_event_payload_carries_model_and_score(tmp_path):
    store, record = _run(tmp_path)
    judge_events = store.list_events(record.run_id, kind="evaluator.judge.invoked")
    assert judge_events
    payload = judge_events[0]["payload"]
    assert payload["evaluator_id"] == "q"
    assert payload["evaluator_kind"] == "judge"
    assert payload["layer"] == 3
    assert payload["scores"][0]["value"] == 4.5
    assert payload["scores"][0]["passed"] is True


def test_provider_call_event_records_prompt_and_output(tmp_path):
    store, record = _run(tmp_path)
    [provider_event, *_] = store.list_events(record.run_id, kind="provider.called", limit=1)
    payload = provider_event["payload"]
    assert "rendered_prompt" in payload
    assert "raw_response" in payload
    # Hashes are populated for both inputs and outputs.
    assert provider_event["inputs_hash"] and len(provider_event["inputs_hash"]) == 64
    assert provider_event["outputs_hash"] and len(provider_event["outputs_hash"]) == 64


# ---------------------------------------------------------------------------
# hash chain


def test_chain_intact_for_clean_run(tmp_path):
    store, record = _run(tmp_path)
    result = verify_chain(store, record.run_id)
    assert result["ok"] is True
    assert result["events"] > 0
    assert result["broken_at"] is None


def test_chain_detects_payload_tampering(tmp_path):
    store, record = _run(tmp_path)
    # Mutate one event's payload directly in the DB — bypassing the emitter.
    raw = sqlite3.connect(tmp_path / "local.db")
    raw.execute(
        "UPDATE events SET payload_json='{\"tampered\":true}' "
        "WHERE kind='evaluator.judge.invoked' LIMIT 1"
    )
    raw.commit()
    raw.close()
    result = verify_chain(store, record.run_id)
    assert result["ok"] is False
    assert "event_hash mismatch" in result["reason"]


def test_chain_detects_event_deletion(tmp_path):
    """Deleting an event breaks ``prev_event_hash`` of the next one."""
    store, record = _run(tmp_path)
    raw = sqlite3.connect(tmp_path / "local.db")
    raw.execute("DELETE FROM events WHERE kind='trial.started' LIMIT 1")
    raw.commit()
    raw.close()
    result = verify_chain(store, record.run_id)
    assert result["ok"] is False
    assert "prev_event_hash mismatch" in result["reason"]


def test_chains_isolated_per_run_id(tmp_path):
    """Runs do not share a chain — concurrent runs should not contend."""
    store_a, run_a = _run(tmp_path / "a")
    store_b, run_b = _run(tmp_path / "b")
    a_events = store_a.list_events(run_a.run_id)
    b_events = store_b.list_events(run_b.run_id)
    # First event in each chain has no predecessor.
    assert a_events[0]["prev_event_hash"] is None
    assert b_events[0]["prev_event_hash"] is None
    # Chain tips differ.
    assert a_events[-1]["event_hash"] != b_events[-1]["event_hash"]


# ---------------------------------------------------------------------------
# privacy / redaction


def test_redact_payload_strips_sensitive_fields_but_keeps_chain(tmp_path):
    store, record = _run(tmp_path, redact=True)
    [provider_event] = store.list_events(record.run_id, kind="provider.called", limit=1)
    payload = provider_event["payload"]
    # rendered_prompt and raw_response replaced by hash stubs
    assert payload["rendered_prompt"]["redacted"] is True
    assert "sha256" in payload["rendered_prompt"]
    assert payload["raw_response"]["redacted"] is True
    # Chain still verifies — the redaction is part of what was hashed.
    result = verify_chain(store, record.run_id)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# actor


def test_actor_falls_back_to_cli_identity(monkeypatch):
    monkeypatch.delenv("EVALGUARD_API_KEY_ID", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.delenv("CI", raising=False)
    actor = resolve_actor()
    assert actor.actor_type == "cli"
    assert actor.actor_id.startswith("cli:")


def test_actor_picks_up_github_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/42/merge")
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    actor = resolve_actor()
    assert actor.actor_type == "ci"
    assert actor.actor_id == "ci:gh:acme/widgets#run/12345"
    assert actor.actor_meta["pr_number"] == "42"
    assert actor.actor_meta["sha"] == "deadbeef"


def test_actor_picks_up_api_key(monkeypatch):
    monkeypatch.setenv("EVALGUARD_API_KEY_ID", "k_abc")
    actor = resolve_actor()
    assert actor.actor_type == "api_key"
    assert actor.actor_id == "api_key:k_abc"


# ---------------------------------------------------------------------------
# JSON contract


def test_serialized_run_with_events_validates_against_schema(tmp_path):
    store, record = _run(tmp_path)
    store.finalize_run(record.run_id, status="passed", gate_status="none")
    payload = run_to_dict(store, record.run_id, include_events=True)
    schema = json.loads(_RUN_SCHEMA_PATH.read_text())
    jsonschema.validate(payload, schema)
    assert payload["audit"]["event_count"] > 0
    assert payload["audit"]["chain_tip"] == payload["audit"]["events"][-1]["event_hash"]


def test_serialized_run_omits_events_by_default(tmp_path):
    store, record = _run(tmp_path)
    store.finalize_run(record.run_id, status="passed", gate_status="none")
    payload = run_to_dict(store, record.run_id)
    assert "audit" not in payload


# ---------------------------------------------------------------------------
# criteria capture — every gate / evaluator records the user-set criteria


def test_judge_event_records_full_spec_and_threshold(tmp_path):
    """For an LLM judge, the audit event must record the model, params,
    threshold, and content-hashed version_id — i.e. the pass/fail
    criteria the user set for this run."""
    store, record = _run(tmp_path)
    [judge_event] = store.list_events(record.run_id, kind="evaluator.judge.invoked", limit=1)
    p = judge_event["payload"]

    assert p["evaluator_id"] == "q"
    assert p["evaluator_kind"] == "judge"
    assert p["evaluator_type"] == "mock_pointwise"
    assert p["threshold"] == 4.0
    # Full configured spec is on the event.
    assert "spec" in p
    assert p["spec"]["id"] == "q"
    assert p["spec"]["score"] == 4.5
    assert p["spec"]["threshold"] == 4.0
    # Content-hashed version_id present on both spec and event subject_version.
    assert p["spec"]["version_id"]
    assert judge_event["subject_version"] == p["spec"]["version_id"]


def test_gate_event_records_full_criteria(tmp_path):
    """gate.evaluated events must record severity / aggregation /
    threshold so an auditor can see exactly what the gate required,
    not just whether it passed."""
    cfg = load_config(_project(tmp_path))
    # Override layers to use a richer threshold + tags so the test
    # covers per_tag_overrides too.
    cfg.raw["layers"] = {
        "judge_offline": {
            "severity": "block",
            "aggregation": "pass_rate_by_tag",
            "threshold": {
                "min": 0.9,
                "per_tag_overrides": {"normal": 0.95, "edge": 0.8},
            },
        },
    }
    store = SqliteStore(tmp_path / "rich.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    # Now run the gate eval the way run_cmd does.
    from evalguard_cli.local.gate import evaluate_gates
    metrics = store.compute_metrics(record.run_id, trial_id=record.trials[0].trial_id)
    gates = evaluate_gates(None, metrics, layers=cfg.raw["layers"])
    store.save_gate_results(record.run_id, gates, trial_id=record.trials[0].trial_id)
    record.audit.emit(
        "gate.evaluated",
        trial_id=record.trials[0].trial_id,
        subject_id=gates[0].name,
        payload={
            "gate_name":   gates[0].name,
            "severity":    gates[0].severity,
            "passed":      gates[0].passed,
            "spec":        cfg.raw["layers"]["judge_offline"],
            "aggregation": cfg.raw["layers"]["judge_offline"]["aggregation"],
            "threshold":   cfg.raw["layers"]["judge_offline"]["threshold"],
        },
    )

    [gate_event] = store.list_events(record.run_id, kind="gate.evaluated", limit=1)
    p = gate_event["payload"]
    assert p["aggregation"] == "pass_rate_by_tag"
    assert p["threshold"]["min"] == 0.9
    assert p["threshold"]["per_tag_overrides"] == {"normal": 0.95, "edge": 0.8}
    assert p["spec"]["severity"] == "block"


def test_heuristic_event_records_evaluator_spec(tmp_path):
    """Heuristic config (e.g. ``length: {max: 600}``) must be on the
    invocation event — not just the resolved score."""
    (tmp_path / "datasets").mkdir(parents=True)
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi"}\n'
    )
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics:\n"
        "  - { type: length, max: 50, unit: chars }\n"
        "  - { type: not_contains, value: 'As an AI' }\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    h_events = store.list_events(record.run_id, kind="evaluator.heuristic.invoked")
    by_type = {e["payload"]["evaluator_type"]: e["payload"] for e in h_events}
    assert by_type["length"]["spec"]["max"] == 50
    assert by_type["length"]["spec"]["unit"] == "chars"
    assert by_type["not_contains"]["spec"]["value"] == "As an AI"


# ---------------------------------------------------------------------------
# secret redaction — API keys must never end up in audit payloads


def test_api_key_in_provider_config_is_redacted(tmp_path, monkeypatch):
    """If the user puts ``api_key: sk-...`` in their YAML config, the
    secret must not materialize in any audit event."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-LIVE-must-not-leak")
    (tmp_path / "datasets").mkdir(parents=True)
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi"}\n'
    )
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config:\n"
        "      api_key: '${OPENAI_API_KEY}'\n"
        "      mode: echo\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    # Walk every event and confirm the live key never appears anywhere.
    raw = sqlite3.connect(tmp_path / "local.db")
    rows = raw.execute("SELECT actor_meta_json, payload_json FROM events").fetchall()
    raw.close()
    for actor_meta, payload in rows:
        assert "sk-LIVE-must-not-leak" not in (actor_meta or "")
        assert "sk-LIVE-must-not-leak" not in (payload or "")
    # And the redaction sentinel is present in the trial.started payload
    # which carries provider_config.
    [trial_started] = store.list_events(record.run_id, kind="trial.started", limit=1)
    assert trial_started["payload"]["provider_config"]["api_key"] == "***"


def test_redactor_handles_nested_secrets():
    from evalguard_cli.local.audit import redact_secrets
    out = redact_secrets({
        "api_key": "secret",
        "model_params": {"temperature": 0, "auth_token": "T"},
        "list_field": [{"password": "p", "ok": "v"}],
        "case_check": {"API-KEY": "secret"},
    })
    assert out["api_key"] == "***"
    assert out["model_params"]["temperature"] == 0
    assert out["model_params"]["auth_token"] == "***"
    assert out["list_field"][0]["password"] == "***"
    assert out["list_field"][0]["ok"] == "v"
    assert out["case_check"]["API-KEY"] == "***"
