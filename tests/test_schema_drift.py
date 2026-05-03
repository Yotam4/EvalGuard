"""Schema-drift canaries.

Each enum the JSON schemas advertise to downstream consumers (UI,
archive, future API client) is mirrored by a constant in code. Drift
between the two surfaces means consumers silently accept values the
runtime never emits, or reject values the runtime does. These tests
pin the pairs.

The ``event.kind`` drift test lives next to the rest of the pipeline
coverage in ``test_pipeline_coverage.py``; this module covers the
remaining vocabulary: ``actor_type``, ``severity``, ``gate_status``.
"""

from __future__ import annotations

import json
from pathlib import Path

from evalguard_cli.local.actor import ACTOR_TYPES
from evalguard_cli.local.gate import GATE_STATUSES, SEVERITIES, THRESHOLD_TYPES


_SCHEMAS = Path(__file__).resolve().parents[1] / "packages" / "schemas"
_RUN_SCHEMA = json.loads((_SCHEMAS / "evalguard.run.schema.json").read_text())
_INPUT_SCHEMA = json.loads((_SCHEMAS / "evalguard.schema.json").read_text())


# ---------------------------------------------------------------------------
# actor_type


def test_actor_type_run_schema_matches_code():
    """``audit.actor_type`` and ``event.actor_type`` enums must exactly
    match ``ACTOR_TYPES`` in code."""
    audit_enum = set(_RUN_SCHEMA["properties"]["audit"]
                                 ["properties"]["actor_type"]["enum"])
    audit_enum.discard(None)  # nullable field permits None
    assert audit_enum == set(ACTOR_TYPES), (
        f"run-schema audit.actor_type {sorted(audit_enum)} != "
        f"ACTOR_TYPES {sorted(ACTOR_TYPES)}"
    )

    event_enum = set(_RUN_SCHEMA["$defs"]["event"]
                                ["properties"]["actor_type"]["enum"])
    assert event_enum == set(ACTOR_TYPES), (
        f"run-schema event.actor_type {sorted(event_enum)} != "
        f"ACTOR_TYPES {sorted(ACTOR_TYPES)}"
    )


# ---------------------------------------------------------------------------
# severity


def test_severity_run_schema_matches_code():
    enum = set(_RUN_SCHEMA["$defs"]["gate"]["properties"]["severity"]["enum"])
    assert enum == set(SEVERITIES), (
        f"run-schema gate.severity {sorted(enum)} != SEVERITIES {sorted(SEVERITIES)}"
    )


def test_severity_input_schema_matches_code():
    layer_enum = set(_INPUT_SCHEMA["$defs"]["layerGate"]
                                  ["properties"]["severity"]["enum"])
    legacy_enum = set(_INPUT_SCHEMA["properties"]["gates"]
                                   ["items"]["properties"]["severity"]["enum"])
    assert layer_enum == set(SEVERITIES), (
        f"input-schema layerGate.severity {sorted(layer_enum)} != "
        f"SEVERITIES {sorted(SEVERITIES)}"
    )
    assert legacy_enum == set(SEVERITIES), (
        f"input-schema legacy gates[*].severity {sorted(legacy_enum)} != "
        f"SEVERITIES {sorted(SEVERITIES)}"
    )


# ---------------------------------------------------------------------------
# gate_status


def test_gate_status_run_schema_matches_code():
    """Both the top-level ``gate_status`` and the per-trial
    ``gate_status`` must enumerate the same values that ``run_cmd``
    actually writes."""
    top = _RUN_SCHEMA["properties"]["gate_status"]
    trial = _RUN_SCHEMA["$defs"]["trial"]["properties"]["gate_status"]
    for label, node in (("run", top), ("trial", trial)):
        enum = set(node["enum"])
        enum.discard(None)
        assert enum == set(GATE_STATUSES), (
            f"run-schema {label}.gate_status {sorted(enum)} != "
            f"GATE_STATUSES {sorted(GATE_STATUSES)}"
        )


# ---------------------------------------------------------------------------
# threshold.type


def test_threshold_type_input_schema_matches_code():
    """Input-schema ``layerGate.threshold.type`` enum must match
    ``THRESHOLD_TYPES``. New threshold mechanisms (statistical,
    relative, future bootstrap CI, etc.) MUST be added in both
    surfaces or the gate engine silently accepts unknown types."""
    enum = set(_INPUT_SCHEMA["$defs"]["layerGate"]
                            ["properties"]["threshold"]
                            ["properties"]["type"]["enum"])
    assert enum == set(THRESHOLD_TYPES), (
        f"input-schema threshold.type {sorted(enum)} != "
        f"THRESHOLD_TYPES {sorted(THRESHOLD_TYPES)}"
    )
