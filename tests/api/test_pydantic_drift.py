"""Drift canary: Pydantic models in apps/api/evalguard_api/models.py
must accept the same payloads the JSON Schema accepts (and reject the
same ones it rejects).

Without this canary the two contracts (server's Pydantic, the rest of
the world's JSON Schema) could silently disagree — e.g. the server
loosens a required field while the schema still requires it, breaking
old clients.

Strategy:

1. Generate a real run JSON via the CLI executor (same source the
   ``evalguard push`` command uses).
2. Validate it against ``evalguard.run.schema.json``.
3. Validate it against ``RunIngest`` Pydantic.
4. Both must accept; the round-tripped Pydantic dump must STILL
   validate against the JSON Schema.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from evalguard_api.models import RunIngest

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


_RUN_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "packages" / "schemas" / "evalguard.run.schema.json"


def _real_run(tmp_path: Path) -> dict:
    base = tmp_path / "cli"
    (base / "datasets").mkdir(parents=True)
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi"}\n{"id":"r2","input":"yo"}\n'
    )
    (base / "evalguard.yaml").write_text(
        "version: 1\nproject: drift_test\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics: [{ id: len, type: length, max: 10000 }]\n"
        "judges: [{ id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }]\n"
    )
    cfg = load_config(base / "evalguard.yaml")
    store = SqliteStore(base / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return run_to_dict(store, record.run_id, include_rows=True, include_scores=True)


def test_real_run_validates_against_both_contracts(tmp_path: Path):
    payload = _real_run(tmp_path)

    # 1. JSON Schema accepts.
    schema = json.loads(_RUN_SCHEMA_PATH.read_text())
    jsonschema.validate(payload, schema)

    # 2. Pydantic accepts.
    parsed = RunIngest.model_validate(payload)

    # 3. Round-trip Pydantic dump still validates against the JSON Schema.
    # ``exclude_unset=True`` mirrors what the ingest endpoint persists —
    # without it, Pydantic materializes optional fields as explicit
    # ``null`` and the JSON Schema's ``additionalProperties: false``
    # definitions (audit / comparison / aggregate are typed ``object``,
    # not nullable) reject the round-trip.
    redumped = parsed.model_dump(mode="json", exclude_unset=True)
    jsonschema.validate(redumped, schema)


def test_pydantic_rejects_missing_required_fields():
    """run_id is required by both contracts."""
    with pytest.raises(ValidationError) as ei:
        RunIngest.model_validate({
            "schema_version": "1.0.0",
            "project": "p",
            "trials": [],
        })
    msg = str(ei.value)
    assert "run_id" in msg


def test_pydantic_rejects_unknown_top_level_fields():
    """``extra='forbid'`` on RunIngest — silent unknown-field
    acceptance was a real risk before."""
    with pytest.raises(ValidationError):
        RunIngest.model_validate({
            "schema_version": "1.0.0",
            "run_id":         "run_test12345678",
            "project":        "p",
            "trials":         [],
            "junk":           "value",
        })


def test_pydantic_rejects_malformed_run_id():
    with pytest.raises(ValidationError):
        RunIngest.model_validate({
            "schema_version": "1.0.0",
            "run_id":         "not-a-run-id",
            "project":        "p",
            "trials":         [],
        })


def test_pydantic_accepts_minimum_run():
    """Smallest valid ingest — no trials, no audit, no comparison."""
    parsed = RunIngest.model_validate({
        "schema_version": "1.0.0",
        "run_id":         "run_minimal000000",
        "project":        "p",
        "trials":         [],
    })
    assert parsed.trials == []
    assert parsed.assets == []
    assert parsed.aggregate is None
    assert parsed.audit is None


def test_event_kind_enum_alignment_via_round_trip(tmp_path: Path):
    """The CLI emits real audit events; the run JSON includes them
    when ``--events`` is set. Verify those events validate under
    Pydantic so we don't accidentally narrow the kind enum."""
    payload = _real_run(tmp_path)
    # Re-fetch with events included to exercise the audit path.
    base = tmp_path / "cli"
    cfg = load_config(base / "evalguard.yaml")
    store = SqliteStore(base / "local.db")
    runs = store.list_runs(limit=10)
    full = run_to_dict(
        store, runs[0]["run_id"],
        include_rows=True, include_scores=True, include_events=True,
    )
    parsed = RunIngest.model_validate(full)
    assert parsed.audit is not None
    assert parsed.audit.event_count > 0
    assert all(e.kind for e in parsed.audit.events)
