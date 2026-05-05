"""Request and response models for ``/v1/runs``.

Designed to mirror ``packages/schemas/evalguard.run.schema.json``
exactly — ``RunIngest`` is the strict ingest shape (rejects unknown
fields so client bugs surface immediately), while ``RunOut`` is the
loose response shape (allows new fields so old clients tolerate
forward-compatible additions).

A drift canary in ``tests/api/test_pydantic_drift.py`` round-trips a
fixture through both the JSON Schema validator and the Pydantic
models so the two sides of the contract can't silently disagree.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Strict-ingest base — every nested model used inside ``RunIngest`` extends this.


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Asset / Score / Gate / Row / Trial — strict shapes for ingest.
# Field names exactly match the JSON Schema; any deviation here would
# silently drop fields client-side OR make the JSON Schema canary fail.


class Asset(_Strict):
    kind:       str
    asset_id:   str
    version_id: str
    source:     str | None = None


class Score(_Strict):
    evaluator_id:   str
    evaluator_kind: str
    layer:          int = Field(ge=1, le=5)
    value:          float
    passed:         bool
    raw:            Any = None


class Row(BaseModel):
    """``additionalProperties: true`` in the schema — clients can add
    domain-specific fields (RAG ``contexts``, text_to_sql
    ``schema_ref``, etc.) without bumping the schema version."""

    model_config = ConfigDict(extra="allow")

    row_id:     str
    passed:     bool
    n_scores:   int = 0
    provider:   str | None = None
    model:      str | None = None
    cost_usd:   float = 0.0
    latency_ms: int = 0
    cache_hit:  bool = False
    tags:       list[str] = Field(default_factory=list)
    input:      Any = None
    expected:   Any = None
    output:     str | None = None
    scores:     list[Score] = Field(default_factory=list)


class GateDetail(BaseModel):
    """Gate details are polymorphic (absolute / relative / ttest /
    custom_check / skip / config-error) — keep ``extra='allow'``."""
    model_config = ConfigDict(extra="allow")


class Gate(_Strict):
    gate_name: str
    severity:  str
    blocking:  bool | None = None
    passed:    bool
    layer:     int | None = Field(default=None, ge=1, le=5)
    details:   list[dict[str, Any]] = Field(default_factory=list)


class Trial(_Strict):
    trial_id:          str
    provider_id:       str
    provider:          str
    model:             str
    prompt_id:         str | None = None
    prompt_version_id: str | None = None
    config:            dict[str, Any] = Field(default_factory=dict)
    row_count:         int = 0
    row_pass_count:    int = 0
    row_fail_count:    int = 0
    cost_usd:          float = 0.0
    status:            str | None = None
    gate_status:       str | None = None
    started_at:        str | None = None
    finished_at:       str | None = None
    metrics:           dict[str, Any] = Field(default_factory=dict)
    gates:             list[Gate] = Field(default_factory=list)
    rows:              list[Row] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level run shapes


class WinnerEntry(_Strict):
    trial_id:    str
    provider_id: str
    value:       float


class Winner(_Strict):
    winner:       WinnerEntry
    runner_up:    WinnerEntry
    lower_better: bool | None = None


class Comparison(_Strict):
    best_by: dict[str, Winner] = Field(default_factory=dict)
    trials:  list[str] = Field(default_factory=list)


class Aggregate(_Strict):
    metrics: dict[str, Any] = Field(default_factory=dict)
    gates:   list[Gate] = Field(default_factory=list)


class AuditEvent(BaseModel):
    """Audit events are open-shape — many event kinds, varied
    payloads. The hash chain is verified against the canonical
    serialization, not field-by-field, so we don't need strict
    typing here."""
    model_config = ConfigDict(extra="allow")

    event_id:        str
    kind:            str
    run_id:          str
    actor_id:        str
    actor_type:      str
    started_at:      str
    event_hash:      str


class Audit(_Strict):
    actor_id:    str | None = None
    actor_type:  str | None = None
    actor_meta:  dict[str, Any] = Field(default_factory=dict)
    trace_id:    str | None = None
    event_count: int = 0
    chain_tip:   str | None = None
    events:      list[AuditEvent] = Field(default_factory=list)


class RunIngest(_Strict):
    """Strict ingest body. Mirrors evalguard.run.schema.json.

    The client (``evalguard push``) sends ``run_to_dict()`` output
    verbatim; this shape locks the contract.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    run_id:         str = Field(pattern=r"^run_[a-z0-9]{8,}$")
    project:        str
    config_hash:    str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    status:         str | None = None
    row_status:     str | None = None
    gate_status:    str | None = None
    started_at:     str | None = None
    finished_at:    str | None = None
    row_count:      int = 0
    row_pass_count: int = 0
    row_fail_count: int = 0
    cost_usd:       float = 0.0
    assets:         list[Asset] = Field(default_factory=list)
    trials:         list[Trial] = Field(default_factory=list)
    comparison:     Comparison | None = None
    aggregate:      Aggregate | None = None
    audit:          Audit | None = None


class RunOut(_Loose):
    """Loose response shape so old clients tolerate new fields.

    The body is what the CLI's ``run_to_dict`` produces, plus the
    server-injected ``server`` envelope (ingested_at, ingested_by,
    project_id) so an operator can see who pushed what when.
    """

    schema_version: str
    run_id:         str
    project:        str
    config_hash:    str | None = None
    trials:         list[dict[str, Any]] = Field(default_factory=list)
    server:         dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Listing


class RunSummary(_Loose):
    run_id:        str
    project:       str
    status:        str | None = None
    gate_status:   str | None = None
    started_at:    str | None = None
    finished_at:   str | None = None
    row_count:     int = 0
    row_pass_count: int = 0
    row_fail_count: int = 0
    cost_usd:      float = 0.0
    ingested_at:   str | None = None
    ingested_by:   str | None = None


class RunList(_Strict):
    runs: list[RunSummary]
    next: str | None = None


# ---------------------------------------------------------------------------
# Org / Project / API-key resources
#
# Slugs are URL-safe lowercase identifiers (a-z, 0-9, hyphen). Names
# are free-form display text. Both shapes are picked to round-trip
# cleanly through CLI / UI / OpenAPI without escaping.

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class OrgCreate(_Strict):
    slug: str = Field(pattern=_SLUG_PATTERN,
                      description="URL-safe identifier; lowercase, hyphens, ≤63 chars.")
    name: str = Field(min_length=1, max_length=200)


class OrgOut(_Strict):
    org_id:     str
    slug:       str
    name:       str
    created_at: str


class ProjectCreate(_Strict):
    slug: str = Field(pattern=_SLUG_PATTERN)
    name: str | None = Field(default=None, max_length=200)


class ProjectOut(_Strict):
    project_id: str
    org_id:     str
    slug:       str
    name:       str
    created_at: str


class ApiKeyCreate(_Strict):
    name:   str = Field(min_length=1, max_length=200,
                        description="Human label; not a secret.")
    scopes: list[str] = Field(default_factory=list,
                              description="e.g. ['admin']. Empty = org-scoped.")


class ApiKeySummary(_Strict):
    """Listing shape — never carries the plaintext token."""

    key_id:        str
    org_id:        str
    prefix:        str
    name:          str
    scopes:        list[str]
    created_at:    str
    revoked_at:    str | None = None
    last_used_at:  str | None = None


class ApiKeyCreated(_Strict):
    """The ``POST`` response — INCLUDES the plaintext token, exactly
    once. Clients must capture it; the server never reveals it again."""

    key:   ApiKeySummary
    token: str = Field(description="Plaintext bearer token. Save it now — it is not retrievable later.")


class OrgList(_Strict):
    orgs: list[OrgOut]


class ProjectList(_Strict):
    projects: list[ProjectOut]


class ApiKeyList(_Strict):
    keys: list[ApiKeySummary]
