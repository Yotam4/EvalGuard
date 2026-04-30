"""Stable JSON shape for a run.

This is the contract every consumer downstream of the executor sees:

- ``evalguard view <run_id> --json``           CLI output
- ``evalguard/action`` PR-comment payload      (Phase 1)
- ``GET /v1/runs/{id}``                        future API endpoint
- Next.js dashboard                            future UI
- Webhooks / S3 archive                        downstream integrations

The shape is described by ``packages/schemas/evalguard.run.schema.json``;
breaking changes bump ``schema_version``.
"""

from __future__ import annotations

import json
from typing import Any

from evalguard_cli.local.sqlite_store import SqliteStore

SCHEMA_VERSION = "1.0.0"


def run_to_dict(
    store: SqliteStore,
    run_id: str,
    *,
    include_rows: bool = True,
    include_scores: bool = False,
    include_events: bool = False,
) -> dict[str, Any]:
    """Materialize a run as the canonical JSON-serializable dict.

    ``include_rows`` adds a ``rows`` array per trial (one entry per
    dataset row × trial). ``include_scores`` adds a ``scores`` array
    per row — heavy, opt-in for full drill-down exports.
    ``include_events`` adds the full audit timeline as a top-level
    ``events`` array — heavy, opt-in for archival exports.
    """
    runs = store.list_runs(limit=1000)
    run = next((r for r in runs if r["run_id"] == run_id), None)
    if run is None:
        raise LookupError(f"run not found: {run_id}")

    trials = store.list_trials(run_id)
    comparison = store.compute_comparison(run_id)
    aggregate_metrics = store.compute_metrics(run_id)
    aggregate_gates = store.get_gate_results(run_id)  # trial_id IS NULL

    trial_objs: list[dict[str, Any]] = []
    for t in trials:
        metrics = store.compute_metrics(run_id, trial_id=t["trial_id"])
        gates = store.get_gate_results(run_id, trial_id=t["trial_id"])
        trial_obj: dict[str, Any] = {
            "trial_id":          t["trial_id"],
            "provider_id":       t["provider_id"],
            "provider":          t["provider"],
            "model":             t["model"],
            "prompt_id":         t["prompt_id"],
            "prompt_version_id": t["prompt_version_id"],
            "config":            t["config"],
            "row_count":         t["row_count"],
            "row_pass_count":    t["row_pass_count"],
            "row_fail_count":    t["row_fail_count"],
            "cost_usd":          t["cost_usd"],
            "status":            t["status"],
            "gate_status":       t["gate_status"],
            "started_at":        t["started_at"],
            "finished_at":       t["finished_at"],
            "metrics":           _strip_nonjson(metrics),
            "gates":             _normalize_gates(gates),
        }
        if include_rows:
            rows = store.list_rows(run_id, trial_id=t["trial_id"])
            trial_rows = [_normalize_row(r, store, run_id, t["trial_id"], include_scores)
                          for r in rows]
            trial_obj["rows"] = trial_rows
        trial_objs.append(trial_obj)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id":         run_id,
        "project":        run["project"],
        "config_hash":    run["config_hash"],
        "status":         run["status"],
        "row_status":     run["row_status"],
        "gate_status":    run["gate_status"],
        "started_at":     run["started_at"],
        "finished_at":    run["finished_at"],
        "row_count":      run["row_count"],
        "row_pass_count": run["row_pass_count"],
        "row_fail_count": run["row_fail_count"],
        "cost_usd":       run["cost_usd"],
        "assets":         json.loads(run["assets_json"] or "[]"),
        "trials":         trial_objs,
        "comparison":     comparison,
        "aggregate": {
            "metrics": _strip_nonjson(aggregate_metrics),
            "gates":   _normalize_gates(aggregate_gates),
        },
    }
    if include_events:
        events = store.list_events(run_id)
        payload["audit"] = {
            "actor_id":     events[0]["actor_id"]   if events else None,
            "actor_type":   events[0]["actor_type"] if events else None,
            "actor_meta":   events[0]["actor_meta"] if events else {},
            "trace_id":     events[0]["trace_id"]   if events else None,
            "event_count":  len(events),
            "chain_tip":    events[-1]["event_hash"] if events else None,
            "events":       events,
        }
    return payload


def run_to_json(store: SqliteStore, run_id: str, **kwargs: Any) -> str:
    """Convenience: pretty-printed JSON string ready to pipe to ``jq``."""
    return json.dumps(run_to_dict(store, run_id, **kwargs), indent=2, default=str)


# ---------------------------------------------------------------------------


def _normalize_gates(gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "gate_name": g["gate_name"],
            "severity":  g.get("severity") or ("block" if g["blocking"] else "warn"),
            "blocking":  g["blocking"],
            "passed":    g["passed"],
            "layer":     g.get("layer"),
            "details":   g.get("details", []),
        }
        for g in gates
    ]


def _normalize_row(
    row: dict[str, Any],
    store: SqliteStore,
    run_id: str,
    trial_id: str,
    include_scores: bool,
) -> dict[str, Any]:
    out = {
        "row_id":     row["row_id"],
        "trial_id":   row.get("trial_id"),
        "passed":     row["passed"],
        "n_scores":   row["n_scores"],
        "provider":   row["provider"],
        "model":      row["model"],
        "cost_usd":   row["cost_usd"],
        "latency_ms": row["latency_ms"],
        "cache_hit":  row["cache_hit"],
        "tags":       row["tags"],
    }
    if include_scores:
        full = store.get_row(run_id, row["row_id"], trial_id=trial_id)
        if full:
            out["input"]    = full["input"]
            out["expected"] = full["expected"]
            out["output"]   = full["output"]
            out["scores"]   = full["scores"]
    return out


def _strip_nonjson(d: dict[str, Any]) -> dict[str, Any]:
    """Drop any value whose JSON-serialization would crash; keep the rest.

    SQL aggregations sometimes return ``Decimal`` or ``None`` in places
    callers don't expect. Defensive coercion keeps the contract stable.
    """
    out: dict[str, Any] = {}
    for k, v in d.items():
        try:
            json.dumps(v, default=str)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)
    return out
