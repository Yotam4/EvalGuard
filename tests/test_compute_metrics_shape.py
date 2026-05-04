"""Pin the contract of ``SqliteStore.compute_metrics``.

This dict is the input to gates, the input to baselines, and (via
``serializer.run_to_dict``) the run-output JSON contract. Drift in
its shape silently breaks gate evaluation, baseline diffs, and the
PR-comment renderer. These tests pin the keys downstream callers rely
on.

The actual values are tested elsewhere (test_layered_gates,
test_pipeline_coverage, etc.); this file only pins the *shape*.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


def _run_simple(tmp_path: Path) -> tuple[SqliteStore, "object"]:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi","tags":["normal"]}\n'
        '{"id":"r2","input":"yo","tags":["edge"]}\n'
    )
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics: [{ id: len, type: length, max: 10000 }]\n"
        "judges: [{ id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }]\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return store, record


# ---------------------------------------------------------------------------


def test_compute_metrics_return_shape(tmp_path: Path):
    """The structured keys gates depend on must always be present, even
    when the metric category is empty (so gate code doesn't need
    .get('by_layer', {}) defensive plumbing)."""
    store, record = _run_simple(tmp_path)
    m = store.compute_metrics(record.run_id)
    # Flat scalars
    assert "row_count" in m
    assert "cost_usd" in m
    assert "pass_rate" in m
    # Structured sub-dicts
    assert "by_evaluator" in m and isinstance(m["by_evaluator"], dict)
    assert "by_layer"     in m and isinstance(m["by_layer"], dict)
    assert "by_tag"       in m and isinstance(m["by_tag"], dict)
    # Schema 1.0.0+ samples — Tier C.2 contract
    assert "samples"      in m and isinstance(m["samples"], dict)


def test_compute_metrics_samples_keyed_by_evaluator(tmp_path: Path):
    """Samples must be keyed by ``evaluator_id`` so threshold.type=ttest
    gates can resolve ``samples[<evaluator>]`` without remapping."""
    store, record = _run_simple(tmp_path)
    m = store.compute_metrics(record.run_id)
    # Both configured evaluators contribute samples.
    assert "len" in m["samples"]
    assert "q"   in m["samples"]
    # One value per row (2 rows here).
    assert len(m["samples"]["len"]) == 2
    assert len(m["samples"]["q"])   == 2
    # All values are floats.
    assert all(isinstance(v, float) for v in m["samples"]["len"])


def test_compute_metrics_by_evaluator_carries_kind_layer_and_n(tmp_path: Path):
    """Gate code keys off ``by_evaluator[id].layer`` (LAYER_INDEX
    reverse lookup) and ``mean`` / ``pass_rate``."""
    store, record = _run_simple(tmp_path)
    m = store.compute_metrics(record.run_id)
    for ev_id, agg in m["by_evaluator"].items():
        assert {"kind", "layer", "mean", "pass_rate", "n", "fail_count"} <= agg.keys()
        assert isinstance(agg["layer"], int)


def test_compute_metrics_by_layer_carries_pass_rate_and_evaluators(tmp_path: Path):
    """``layer.pass_rate_by_tag`` aggregation in gate.py walks
    ``by_layer[idx]`` for the layer roll-up."""
    store, record = _run_simple(tmp_path)
    m = store.compute_metrics(record.run_id)
    for layer, agg in m["by_layer"].items():
        assert isinstance(layer, int)
        assert {"mean", "pass_rate", "row_pass_rate", "rows_evaluated", "n", "evaluators"} <= agg.keys()


def test_compute_metrics_trial_scope(tmp_path: Path):
    """Trial-scoped metrics must have the same shape as run-level."""
    store, record = _run_simple(tmp_path)
    [trial] = store.list_trials(record.run_id)
    m = store.compute_metrics(record.run_id, trial_id=trial["trial_id"])
    for key in ("row_count", "cost_usd", "pass_rate",
                "by_evaluator", "by_layer", "by_tag", "samples"):
        assert key in m, f"trial-scoped metrics missing {key!r}"


# ---------------------------------------------------------------------------
# Empty-state — pin the shape when nothing has been recorded.


def test_compute_metrics_empty_run_returns_full_shape(tmp_path: Path):
    """A run with no scores still must return the full key set so
    callers can rely on ``metrics['by_evaluator']`` indexing without
    a ``.get`` dance."""
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    store.start_run("run_empty00000000", "t", "0" * 64, assets=[])
    m = store.compute_metrics("run_empty00000000")
    assert m["row_count"] == 0.0
    assert m["pass_rate"] == 0.0
    assert m["by_evaluator"] == {}
    assert m["by_layer"]     == {}
    assert m["by_tag"]       == {}
    assert m["samples"]      == {}
