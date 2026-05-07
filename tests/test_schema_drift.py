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
from evalguard_cli.local.gate import (
    GATE_STATUSES,
    SEVERITIES,
    SUPPORTED_AGGREGATIONS,
    THRESHOLD_TYPES,
)
from evalguard_cli.local.retry import _DEFAULT_RETRY_PATTERNS, RetryPolicy


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


# ---------------------------------------------------------------------------
# aggregation
#
# The schema's enum is intentionally a *superset* of what the runtime
# implements (forward-compat slot for ``p50`` / ``p95`` percentile
# rollups). The drift test pins the gap so the not-implemented set
# stays explicit; if the runtime adds ``p50`` later it must drop out
# of ``advertised_only``. Conversely, if the runtime ever supports
# something the schema doesn't advertise, the gate engine would
# accept a YAML that fails JSON-schema validation — also a bug.


def test_aggregation_input_schema_superset_of_supported():
    advertised = set(_INPUT_SCHEMA["$defs"]["layerGate"]
                                  ["properties"]["aggregation"]["enum"])
    supported  = set(SUPPORTED_AGGREGATIONS)
    runtime_only = supported - advertised
    advertised_only = advertised - supported
    assert not runtime_only, (
        f"runtime supports aggregations the schema does not advertise: "
        f"{sorted(runtime_only)}. Either add to the schema enum or "
        f"drop from SUPPORTED_AGGREGATIONS."
    )
    # ``p50`` / ``p95`` are deliberately advertised-only today. If new
    # not-yet-implemented values show up, fail loudly so we stay
    # honest about the gap.
    expected_advertised_only = {"p50", "p95"}
    assert advertised_only == expected_advertised_only, (
        f"schema advertises aggregations the runtime does not "
        f"implement: {sorted(advertised_only)}; expected exactly "
        f"{sorted(expected_advertised_only)}. If you implemented "
        f"one, add it to ``SUPPORTED_AGGREGATIONS``."
    )


# ---------------------------------------------------------------------------
# retry block defaults


def test_retry_input_schema_defaults_match_RetryPolicy():
    """Schema-advertised defaults under ``retry:`` must match
    ``RetryPolicy`` field defaults. Drift here means a user reading
    the JSON schema sees one value while the runtime applies another
    when the key is omitted from YAML."""
    schema_props = _INPUT_SCHEMA["properties"]["retry"]["properties"]
    pol = RetryPolicy()
    assert schema_props["max_retries"]["default"]   == pol.max_retries
    assert schema_props["base_delay_ms"]["default"] == pol.base_delay_ms
    assert schema_props["max_delay_ms"]["default"]  == pol.max_delay_ms
    assert schema_props["jitter"]["default"]        == pol.jitter
    # ``retry_on`` has no schema default (the description points at
    # ``_DEFAULT_RETRY_PATTERNS``); just sanity-check the runtime
    # default is non-empty so future refactors can't silently empty
    # the list.
    assert tuple(_DEFAULT_RETRY_PATTERNS) == pol.retry_on
    assert pol.retry_on, "default retry_on must not be empty"
