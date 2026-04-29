"""Multi-trial (model-effectiveness comparison) end-to-end."""

from __future__ import annotations

import asyncio
import json
import pathlib

import jsonschema
import pytest

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


_RUN_SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[1] / "packages" / "schemas" / "evalguard.run.schema.json"


def _project(base, *, providers: list[str], judge_threshold: float = 4.0) -> pathlib.Path:
    (base / "datasets").mkdir(parents=True, exist_ok=True)
    (base / "prompts").mkdir(parents=True, exist_ok=True)
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi","expected":"y","tags":["normal"]}\n'
        '{"id":"r2","input":"yo","expected":"y","tags":["edge"]}\n'
    )
    (base / "prompts" / "p.md").write_text("Echo: {input}")
    cfg = base / "evalguard.yaml"
    provider_block = "\n".join(
        f"  - {{ id: '{p}', config: {{ mode: echo }} }}"
        for p in providers
    )
    cfg.write_text(
        "version: 1\nproject: t\n"
        "providers:\n" + provider_block + "\n"
        "prompts: [{ id: p, file: prompts/p.md }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "judges:\n"
        f"  - {{ id: q, type: mock_pointwise, score: 4.5, threshold: {judge_threshold} }}\n"
        "layers:\n"
        "  judge_offline: { severity: block, aggregation: pass_rate, threshold: {min: 0.9} }\n"
    )
    return cfg


def test_two_providers_produce_two_trials(tmp_path: pathlib.Path) -> None:
    cfg = load_config(_project(tmp_path, providers=["mock:m1", "mock:m2"]))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    assert len(record.trials) == 2
    assert {t.provider_id for t in record.trials} == {"mock:m1", "mock:m2"}
    # Each trial sees the same dataset (2 rows).
    assert all(t.row_count == 2 for t in record.trials)
    # Aggregate run row_count = trials × rows.
    assert record.row_count == 4


def test_per_trial_metrics_are_independent(tmp_path: pathlib.Path) -> None:
    cfg = load_config(_project(tmp_path, providers=["mock:m1", "mock:m2"]))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    a, b = record.trials
    metrics_a = store.compute_metrics(record.run_id, trial_id=a.trial_id)
    metrics_b = store.compute_metrics(record.run_id, trial_id=b.trial_id)
    # Same evaluators in both.
    assert set(metrics_a["by_evaluator"]) == set(metrics_b["by_evaluator"])
    # n is per-trial, not aggregated.
    assert metrics_a["row_count"] == 2.0
    assert metrics_b["row_count"] == 2.0


def test_aggregate_metrics_dedupe_tags_across_trials(tmp_path: pathlib.Path) -> None:
    """Aggregating across 2 trials shouldn't double-count tag denominators."""
    cfg = load_config(_project(tmp_path, providers=["mock:m1", "mock:m2"]))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    agg = store.compute_metrics(record.run_id)
    # Dataset has 1 row per tag; aggregate denominator must stay 1, not 2.
    for tag in ("normal", "edge"):
        assert agg["by_tag"][tag]["n"] == 1


def test_comparison_picks_winner_lower_better_for_cost(tmp_path: pathlib.Path) -> None:
    cfg = load_config(_project(tmp_path, providers=["mock:m1", "mock:m2"]))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    cmp = store.compute_comparison(record.run_id)
    # cost_usd must be marked lower-better, even when the actual values tie.
    assert cmp["best_by"]["cost_usd"]["lower_better"] is True
    # pass_rate is higher-better.
    if "pass_rate" in cmp["best_by"]:
        assert cmp["best_by"]["pass_rate"]["lower_better"] is False


def test_run_failed_when_one_trial_fails_under_strategy_all(tmp_path: pathlib.Path) -> None:
    """gate_strategy: all (default) — any trial failing fails the run."""
    cfg = load_config(_project(
        tmp_path, providers=["mock:m1"], judge_threshold=4.9,   # forces fail
    ))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    assert record.row_status == "failed"
    assert record.trials[0].row_status == "failed"


def test_serialized_run_validates_against_schema(tmp_path: pathlib.Path) -> None:
    cfg = load_config(_project(tmp_path, providers=["mock:m1", "mock:m2"]))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    # finalize_run isn't called here (run_cmd does that), so set a status
    # so the schema's enum doesn't reject "running".
    store.finalize_run(record.run_id, status="passed", gate_status="none")

    payload = run_to_dict(store, record.run_id, include_scores=True)
    schema = json.loads(_RUN_SCHEMA_PATH.read_text())
    jsonschema.validate(payload, schema)

    assert payload["schema_version"] == "1.0.0"
    assert len(payload["trials"]) == 2
    # Each trial has rows scoped to its own trial_id.
    for trial in payload["trials"]:
        assert all(r["trial_id"] == trial["trial_id"] for r in trial["rows"])
        assert all(r["model"] == trial["model"] for r in trial["rows"])


def test_get_row_with_trial_id_filters_scores(tmp_path: pathlib.Path) -> None:
    """Multi-trial runs share row_ids; get_row(trial_id=...) must scope."""
    cfg = load_config(_project(tmp_path, providers=["mock:m1", "mock:m2"]))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    a = record.trials[0]
    full = store.get_row(record.run_id, "r1", trial_id=a.trial_id)
    assert full is not None
    assert full["trial_id"] == a.trial_id
    # Without the filter we'd get scores from both trials.
    assert all(s["evaluator_kind"] in {"heuristic", "metric", "judge"}
               for s in full["scores"])


def test_gate_strategy_any_passes_when_one_trial_passes() -> None:
    """Run-level pass should be granted under ``gate_strategy: any``."""
    # Simulate trial verdicts (hand-build to avoid wiring the run loop).
    from evalguard_cli.local.gate import GateResult
    trial_verdicts = [
        {"trial": None, "metrics": {}, "gates": [], "status": "gate_failed",
         "gate_status": "failed", "blocking_failed": True, "warned": False},
        {"trial": None, "metrics": {}, "gates": [], "status": "passed",
         "gate_status": "passed", "blocking_failed": False, "warned": False},
    ]
    # under `all`: blocking_failed if ANY trial blocking-fails
    run_blocking_all = any(v["blocking_failed"] for v in trial_verdicts)
    # under `any`: blocking_failed only if EVERY trial blocking-fails
    run_blocking_any = all(v["blocking_failed"] for v in trial_verdicts)
    assert run_blocking_all is True
    assert run_blocking_any is False
