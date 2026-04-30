"""Audit log: emission, hash chain integrity, tamper detection, redaction."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3

import jsonschema
import pytest

from evalguard_cli.local.actor import Actor, resolve_actor
from evalguard_cli.local.audit import AuditLog, verify_chain
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
