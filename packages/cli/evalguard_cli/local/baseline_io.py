"""Baseline files: snapshot a run's metrics for relative-threshold gating.

A baseline is a small, self-describing JSON document that captures the
metrics shape of one run. It's the unit of comparison the Phase 1
GitHub Action passes between the ``main`` branch and a PR run, but
it works just as well for local "current vs last week" comparisons.

The shape is pinned by ``packages/schemas/evalguard.baseline.schema.json``
so consumers (the Action, future server import endpoints, downstream
dashboards) can validate and refuse drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema

BASELINE_SCHEMA_VERSION = "1.0.0"


@dataclass
class Baseline:
    schema_version: str
    run_id: str
    config_hash: str
    saved_at: str
    metrics: dict[str, Any]
    actor_meta: dict[str, Any] = field(default_factory=dict)


def save_baseline(
    *,
    run_id: str,
    config_hash: str,
    metrics: dict[str, Any],
    actor_meta: dict[str, Any] | None = None,
    path: Path,
) -> Baseline:
    """Atomically write a baseline file. Returns the in-memory record."""
    baseline = Baseline(
        schema_version=BASELINE_SCHEMA_VERSION,
        run_id=run_id,
        config_hash=config_hash,
        saved_at=_now_iso(),
        metrics=_jsonable(metrics),
        actor_meta=actor_meta or {},
    )
    payload = {
        "schema_version": baseline.schema_version,
        "run_id":         baseline.run_id,
        "config_hash":    baseline.config_hash,
        "saved_at":       baseline.saved_at,
        "actor_meta":     baseline.actor_meta,
        "metrics":        baseline.metrics,
    }
    _validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)
    return baseline


def load_baseline(path: Path) -> Baseline:
    if not path.exists():
        raise FileNotFoundError(f"baseline file not found: {path}")
    payload = json.loads(path.read_text())
    _validate(payload)
    return Baseline(
        schema_version=payload["schema_version"],
        run_id=payload["run_id"],
        config_hash=payload["config_hash"],
        saved_at=payload["saved_at"],
        metrics=payload["metrics"],
        actor_meta=payload.get("actor_meta") or {},
    )


# ---------------------------------------------------------------------------


def _validate(payload: dict[str, Any]) -> None:
    schema = json.loads(_schema_path().read_text())
    jsonschema.validate(payload, schema)


def _schema_path() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packages" / "schemas" / "evalguard.baseline.schema.json"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("evalguard.baseline.schema.json not found")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    """Coerce metric-dict values that aren't JSON-native (e.g. dict keys
    coming from SQL aggregations as ints) into JSON-friendly forms."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value
