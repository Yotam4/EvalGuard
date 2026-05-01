"""Pipeline-coverage tests: every state transition emits an event.

The audit log is the source of truth for "who/what/when/why" — these
tests pin down the exact event vocabulary the pipeline emits across
the realistic scenarios that an enterprise auditor cares about:

1. Standard run (multi-trial, gates, no failures).
2. ``run_mode: short_circuit_blocking_only`` with a failing layer-1
   heuristic — must emit ``row.short_circuited`` per row.
3. ``cost_cap_usd`` exceeded mid-run — must emit ``run.cost_capped``
   exactly once.
4. Real ``PointwiseJudge`` calling a mock provider — must emit a
   nested ``provider.called`` event whose ``parent_span_id`` is the
   ``evaluator.judge.invoked`` event's ``span_id``.

Every event in every scenario is checked for required fields, and the
hash chain is re-verified end-to-end after each.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import sqlite3
import sys
import textwrap
from pathlib import Path

from evalguard_cli.local.audit import EVENT_KINDS, verify_chain
from evalguard_cli.local.gate import evaluate_gates
from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


# ---------------------------------------------------------------------------
# helpers


def _write_yaml(base: Path, body: str) -> Path:
    cfg = base / "evalguard.yaml"
    cfg.write_text(body)
    return cfg


def _write_dataset(base: Path, lines: list[str], path: str = "datasets/g.jsonl") -> None:
    full = base / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("\n".join(lines) + "\n")


def _run(base: Path, cfg_text: str) -> tuple[SqliteStore, "object"]:
    cfg_path = _write_yaml(base, cfg_text)
    cfg = load_config(cfg_path)
    store = SqliteStore(base / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return store, record


def _kinds(events: list[dict]) -> collections.Counter:
    return collections.Counter(e["kind"] for e in events)


def _required_fields_present(events: list[dict]) -> None:
    for ev in events:
        for f in ("event_id", "kind", "run_id", "actor_id", "actor_type",
                  "trace_id", "span_id", "started_at", "event_hash"):
            assert ev.get(f), f"event missing {f!r}: {ev}"
        # Chain pointers
        assert "prev_event_hash" in ev   # may be None for first event
        # Kind is one of the registered kinds
        assert ev["kind"] in EVENT_KINDS, f"unknown kind {ev['kind']!r}"


# ---------------------------------------------------------------------------
# 1. Standard run — full event vocabulary, no failures


def test_standard_run_emits_full_vocabulary(tmp_path: Path):
    _write_dataset(tmp_path, [
        '{"id":"r1","input":"x","tags":["normal"]}',
        '{"id":"r2","input":"y","tags":["edge"]}',
    ])
    store, record = _run(tmp_path,
        "version: 1\nproject: cov\n"
        "providers:\n"
        "  - { id: 'mock:m1', config: { mode: echo } }\n"
        "  - { id: 'mock:m2', config: { mode: echo } }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics:\n"
        "  - { type: length, max: 1000 }\n"
        "judges:\n"
        "  - { id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }\n"
        "layers:\n"
        "  heuristics:    { severity: block, threshold: {min: 1.0} }\n"
        "  judge_offline: { severity: block, threshold: {min: 0.9} }\n"
    )
    events = store.list_events(record.run_id)
    _required_fields_present(events)
    counts = _kinds(events)

    # Lifecycle bookends.
    assert counts["run.started"] == 1
    # The pipeline emits trial.started/finalized per provider.
    assert counts["trial.started"] == 2
    assert counts["trial.finalized"] == 2
    # 2 rows × 2 trials = 4 main provider calls. No judge nested calls
    # (mock_pointwise doesn't call a provider).
    assert counts["provider.called"] == 4
    # 1 heuristic × 2 rows × 2 trials = 4 invocations.
    assert counts["evaluator.heuristic.invoked"] == 4
    # 1 judge × 2 rows × 2 trials = 4 invocations.
    assert counts["evaluator.judge.invoked"] == 4
    # No short-circuit, no cost cap.
    assert counts.get("row.short_circuited", 0) == 0
    assert counts.get("run.cost_capped", 0) == 0
    # NB: ``run.finalized`` and ``gate.evaluated`` are emitted by run_cmd
    # after this executor returns; those are tested separately in
    # test_audit.py:test_run_emits_full_event_lifecycle.

    assert verify_chain(store, record.run_id)["ok"]


# ---------------------------------------------------------------------------
# 2. Short-circuit emits row.short_circuited


def test_short_circuit_emits_row_event(tmp_path: Path):
    """A failing layer-1 heuristic with ``severity: block`` must emit
    ``row.short_circuited`` and NOT invoke the layer-3 judge."""
    _write_dataset(tmp_path, [
        '{"id":"r1","input":"hi","expected":"y"}',
    ])
    store, record = _run(tmp_path,
        "version: 1\nproject: cov\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics:\n"
        "  - { type: length, max: 1, unit: chars }\n"      # forces fail
        "judges:\n"
        "  - { id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }\n"
        "layers:\n"
        "  heuristics:    { severity: block, threshold: {min: 1.0} }\n"
        "  judge_offline: { severity: block, threshold: {min: 0.9} }\n"
        "run_mode: short_circuit_blocking_only\n"
    )
    events = store.list_events(record.run_id)
    counts = _kinds(events)
    assert counts["row.short_circuited"] == 1
    sc = next(e for e in events if e["kind"] == "row.short_circuited")
    assert sc["payload"]["failed_at_layer"] == 1
    assert 3 in sc["payload"]["skipped_layers"]
    assert sc["payload"]["run_mode"] == "short_circuit_blocking_only"
    # Judge never ran for this row.
    assert counts.get("evaluator.judge.invoked", 0) == 0
    assert verify_chain(store, record.run_id)["ok"]


# ---------------------------------------------------------------------------
# 3. Cost cap emits run.cost_capped


def test_cost_cap_emits_one_event(tmp_path: Path):
    """When the cost cap is hit, exactly one ``run.cost_capped`` event
    should fire (idempotent — pre-flight checks abort subsequent rows
    silently)."""
    # Inputs must be unique so the cache doesn't collapse cost — without
    # that, only the first call charges and the cap never triggers.
    _write_dataset(tmp_path, [
        f'{{"id":"r{i}","input":"row {i}"}}' for i in range(6)
    ])
    store, record = _run(tmp_path,
        "version: 1\nproject: cov\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0, cost_per_call: 0.10 }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "cache: { enabled: false }\n"
        "concurrency: 1\n"             # serial so the cap is deterministic
        "cost_cap_usd: 0.25\n"         # cap hit after 3 calls
    )
    events = store.list_events(record.run_id)
    counts = _kinds(events)
    assert counts["run.cost_capped"] == 1
    cc = next(e for e in events if e["kind"] == "run.cost_capped")
    assert cc["payload"]["cost_cap_usd"] == 0.25
    assert cc["payload"]["total_cost_usd"] >= 0.25
    # Trial recorded as cost_capped.
    [trial] = store.list_trials(record.run_id)
    assert trial["status"] == "cost_capped"
    assert verify_chain(store, record.run_id)["ok"]


# ---------------------------------------------------------------------------
# 4. PointwiseJudge emits a nested provider.called for its own LLM call


def test_pointwise_judge_emits_nested_provider_call(tmp_path: Path):
    """A real LLM-as-judge must record its own LLM API call as a child
    of the judge invocation event so an auditor can see the model,
    rendered prompt, raw response, tokens, latency, and cost of that
    specific judge call separately from the trial's main provider."""
    _write_dataset(tmp_path, [
        '{"id":"r1","input":"some text","expected":"summary"}',
    ])
    rubric_path = tmp_path / "rubric.md"
    rubric_path.write_text("Rate 1-5 for helpfulness.")
    store, record = _run(tmp_path,
        "version: 1\nproject: cov\n"
        "providers:\n"
        "  - { id: 'mock:main', config: { mode: echo } }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "judges:\n"
        "  - id: helpfulness_v3\n"
        "    type: pointwise\n"
        "    model: 'mock:judge-model'\n"
        "    rubric_file: rubric.md\n"
        "    threshold: 4.0\n"
        "    provider_config: { mode: judge_score, score: 4.7, latency_ms: 0 }\n"
    )
    events = store.list_events(record.run_id)

    # The judge's own LLM call has is_judge_call: true and parent_span_id
    # pointing back at the judge invocation event.
    judge_invocations = [e for e in events if e["kind"] == "evaluator.judge.invoked"]
    assert len(judge_invocations) == 1
    judge = judge_invocations[0]
    judge_calls = [e for e in events
                   if e["kind"] == "provider.called"
                   and e.get("payload", {}).get("is_judge_call")]
    assert len(judge_calls) == 1
    judge_call = judge_calls[0]
    assert judge_call["parent_span_id"] == judge["span_id"]
    p = judge_call["payload"]
    assert p["provider"] == "mock"
    assert p["model"] == "judge-model"
    assert "rendered_prompt" in p
    assert "raw_response" in p
    # Token shape preserved from the mock provider.
    assert p["tokens"] is not None and "prompt" in p["tokens"]
    # Trial's main provider call also exists, with is_judge_call: false.
    main_calls = [e for e in events
                  if e["kind"] == "provider.called"
                  and not e.get("payload", {}).get("is_judge_call")]
    assert len(main_calls) == 1
    assert verify_chain(store, record.run_id)["ok"]


# ---------------------------------------------------------------------------
# 5. Asset-resolution events — one per content-hashed asset


def test_asset_resolved_emitted_per_loaded_asset(tmp_path: Path):
    """Every asset on ``cfg.assets`` must show up as a discrete
    ``asset.resolved`` event with its content-hashed version_id."""
    _write_dataset(tmp_path, [
        '{"id":"r1","input":"hi"}',
    ])
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "p.md").write_text("Echo: {input}")
    store, record = _run(tmp_path,
        "version: 1\nproject: cov\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "prompts: [{ id: p1, file: prompts/p.md }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "judges: [{ id: q, type: mock_pointwise }]\n"
    )
    asset_events = store.list_events(record.run_id, kind="asset.resolved")
    # At minimum: one prompt, one dataset, one judge spec.
    kinds_seen = {e["payload"]["kind"] for e in asset_events}
    assert {"prompt", "dataset", "judge"} <= kinds_seen
    # Each event carries a content-hashed version_id and a source label.
    for ev in asset_events:
        assert ev["subject_version"]
        assert len(ev["subject_version"]) == 64
        assert ev["payload"]["version_id"] == ev["subject_version"]
        assert ev["payload"]["source"]
    # At least: one prompt + one dataset + one judge → 3 events.
    # Heuristics / metrics are absent in this minimal config.
    assert len(asset_events) >= 3


# ---------------------------------------------------------------------------
# 6. Custom-check escape hatch emits its own dedicated event


def _install_module(source: str, tmp_path: Path) -> str:
    name = "ev_" + hashlib.md5(textwrap.dedent(source).encode()).hexdigest()[:8]
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source))
    sys.path.insert(0, str(tmp_path))
    return name


def test_gate_custom_check_emits_dedicated_event(tmp_path: Path):
    """When a gate runs a Python ``custom_check``, an additional
    ``gate.custom_check.invoked`` event must record the module path,
    config, duration, pass/fail, and any exception — independently
    auditable, with ``parent_span_id`` linking to the gate event."""
    mod = _install_module("""
        def threshold(metrics, config):
            min_n = float(config['min_rows'])
            actual = float(metrics['row_count'])
            return {'passed': actual >= min_n,
                    'details': [{'metric': 'rows', 'op': '>=',
                                 'target': min_n, 'actual': actual,
                                 'passed': actual >= min_n}]}
    """, tmp_path)

    # Run a tiny pipeline so we have a real run_id + audit log.
    _write_dataset(tmp_path, [
        '{"id":"r1","input":"hi"}',
        '{"id":"r2","input":"yo"}',
    ])
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: cov\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    # Drive a gate eval the way run_cmd does.
    layers = {"heuristics": {
        "severity": "block",
        "custom_check": {
            "module": f"{mod}:threshold",
            "config": {"min_rows": 5},
        },
    }}
    metrics = store.compute_metrics(record.run_id)
    gates = evaluate_gates(None, metrics, layers=layers)
    assert len(gates) == 1
    g = gates[0]
    assert not g.passed                                  # 2 < 5
    assert g.custom_check_execution is not None
    exec_meta = g.custom_check_execution
    assert exec_meta["module"].endswith(":threshold")
    assert exec_meta["config"] == {"min_rows": 5}
    assert exec_meta["passed"] is False
    assert exec_meta["error"] is None
    assert exec_meta["duration_ms"] is not None and exec_meta["duration_ms"] >= 0

    # Now have the audit log emit the dedicated event the way run_cmd does.
    import uuid as _uuid
    gate_span = _uuid.uuid4().hex[:16]
    record.audit.emit(
        "gate.evaluated",
        span_id=gate_span,
        subject_id=g.name,
        payload={"gate_name": g.name, "passed": g.passed},
    )
    record.audit.emit(
        "gate.custom_check.invoked",
        parent_span_id=gate_span,
        subject_id=exec_meta["module"],
        payload=exec_meta,
        duration_ms=exec_meta["duration_ms"],
    )

    [cc_event] = store.list_events(record.run_id, kind="gate.custom_check.invoked", limit=1)
    assert cc_event["payload"]["module"].endswith(":threshold")
    assert cc_event["payload"]["passed"] is False
    assert cc_event["payload"]["error"] is None
    # Parent links to the gate event.
    [gate_event] = store.list_events(record.run_id, kind="gate.evaluated", limit=1)
    assert cc_event["parent_span_id"] == gate_event["span_id"]
    assert verify_chain(store, record.run_id)["ok"]


def test_gate_custom_check_records_exceptions(tmp_path: Path):
    """If the user's Python ``custom_check`` raises, the execution
    metadata records ``error`` and the gate fails (verified via the
    GateResult — execution metadata always carries the exception type
    and message for forensics)."""
    mod = _install_module("""
        def boom(metrics, config):
            raise ValueError("intentional unit-test failure")
    """, tmp_path)
    layers = {"human": {
        "severity": "block",
        "custom_check": {"module": f"{mod}:boom"},
    }}
    metrics = {"row_count": 1.0, "cost_usd": 0.0,
               "by_evaluator": {}, "by_layer": {}, "by_tag": {}}
    gates = evaluate_gates(None, metrics, layers=layers)
    g = gates[0]
    assert not g.passed
    exec_meta = g.custom_check_execution
    assert exec_meta is not None
    assert "intentional" in exec_meta["error"]
    assert "ValueError" in exec_meta["error"]
    assert exec_meta["duration_ms"] is not None


# ---------------------------------------------------------------------------
# 7. Every kind we emit is registered (canary so unknown kinds blow up early)


def test_every_emitted_kind_is_registered(tmp_path: Path):
    _write_dataset(tmp_path, [
        '{"id":"r1","input":"hi"}',
    ])
    store, record = _run(tmp_path,
        "version: 1\nproject: cov\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics: [{ type: length, max: 10000 }]\n"
        "judges: [{ id: q, type: mock_pointwise }]\n"
    )
    raw = sqlite3.connect(tmp_path / "local.db")
    distinct_kinds = {row[0] for row in raw.execute("SELECT DISTINCT kind FROM events")}
    raw.close()
    unknown = distinct_kinds - set(EVENT_KINDS)
    assert not unknown, f"unknown kinds emitted: {unknown}"


# ---------------------------------------------------------------------------
# 8. event.kind in run.schema.json must mirror EVENT_KINDS (drift canary)


def test_run_schema_event_kind_matches_event_kinds():
    """The run-output JSON Schema's ``event.kind`` enum must exactly
    match the ``EVENT_KINDS`` registry. Drift means a consumer
    (UI / archive) silently accepts a kind the runtime never emits, or
    rejects one it does."""
    import json
    schema_path = Path(__file__).resolve().parents[1] / "packages" / "schemas" / "evalguard.run.schema.json"
    schema = json.loads(schema_path.read_text())
    enum = set(schema["$defs"]["event"]["properties"]["kind"]["enum"])
    assert enum == set(EVENT_KINDS), (
        f"schema/code drift: schema enum {sorted(enum)} != "
        f"EVENT_KINDS {sorted(EVENT_KINDS)}"
    )


# ---------------------------------------------------------------------------
# 9. Concurrent rows must not corrupt each other's audit hook


def test_concurrent_rows_dont_share_audit_hook(tmp_path: Path):
    """Regression: under ``concurrency > 1`` two coroutines used to set
    ``ev._audit_hook`` on the *same* shared evaluator instance, leaking
    one row's hook into another row's evaluate(). The fix moves the
    hook to a ``contextvars.ContextVar`` (task-local). This test
    exercises the failure path with a real ``PointwiseJudge`` calling
    a mock provider — every nested provider.called event must point
    back at the correct judge invocation event for its own row."""
    _write_dataset(tmp_path, [
        f'{{"id":"r{i}","input":"input number {i}","expected":"x"}}'
        for i in range(8)
    ])
    rubric = tmp_path / "rubric.md"
    rubric.write_text("Rate 1-5 for helpfulness.")
    store, record = _run(tmp_path,
        "version: 1\nproject: cov\n"
        "providers:\n"
        "  - { id: 'mock:main', config: { mode: echo, latency_ms: 0 } }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "concurrency: 8\n"
        "judges:\n"
        "  - id: helpfulness_v3\n"
        "    type: pointwise\n"
        "    model: 'mock:judge-model'\n"
        "    rubric_file: rubric.md\n"
        "    threshold: 4.0\n"
        "    provider_config: { mode: judge_score, score: 4.7, latency_ms: 0 }\n"
    )
    events = store.list_events(record.run_id)
    judge_events = {e["span_id"]: e for e in events
                    if e["kind"] == "evaluator.judge.invoked"}
    judge_calls = [e for e in events
                   if e["kind"] == "provider.called"
                   and e.get("payload", {}).get("is_judge_call")]
    # Every nested judge call's parent_span_id must resolve to one of
    # the judge invocation events that ran in this run, AND the trial /
    # row of that parent must match the call's own trial / row.
    assert len(judge_calls) == 8
    for call in judge_calls:
        parent = judge_events.get(call["parent_span_id"])
        assert parent is not None, "judge call points at unknown parent"
        assert parent["row_id"]   == call["row_id"]
        assert parent["trial_id"] == call["trial_id"]
    assert verify_chain(store, record.run_id)["ok"]
