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

from typing import Any, Literal

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
    ``schema_ref``, etc.) without bumping the schema version.

    Numeric constraints mirror ``$defs.row`` in
    ``evalguard.run.schema.json`` — ``minimum: 0`` on the count
    columns. Without these, an adversarial / buggy client could
    push ``n_scores: -1`` and the JSON Schema would reject it
    while Pydantic silently accepts.
    """

    model_config = ConfigDict(extra="allow")

    row_id:     str
    passed:     bool
    n_scores:   int   = Field(default=0, ge=0)
    provider:   str | None = None
    model:      str | None = None
    cost_usd:   float = Field(default=0.0, ge=0)
    latency_ms: int   = Field(default=0, ge=0)
    cache_hit:  bool  = False
    tags:       list[str] = Field(default_factory=list)
    input:      Any = None
    expected:   Any = None
    output:     str | None = None
    # Cap nested-list cardinality to bound worst-case nested
    # deserialization. Real runs rarely exceed ~20 evaluators per row;
    # 100 is generous headroom.
    scores:     list[Score] = Field(default_factory=list, max_length=100)


class Gate(_Strict):
    gate_name: str
    severity:  str
    blocking:  bool | None = None
    passed:    bool
    layer:     int | None = Field(default=None, ge=1, le=5)
    # ``details`` is intentionally polymorphic — gate detail shapes
    # vary by threshold type (absolute / relative / ttest /
    # custom_check / skip / config-error). Pinning a typed model
    # would force every new threshold type to update Pydantic in
    # lockstep; ``dict[str, Any]`` matches the JSON Schema's
    # ``"items": {}`` and lets the gate engine evolve freely.
    details:   list[dict[str, Any]] = Field(default_factory=list)


class Trial(_Strict):
    trial_id:          str = Field(pattern=r"^trial_[a-z0-9]{8,}$")
    provider_id:       str
    provider:          str
    model:             str
    prompt_id:         str | None = None
    prompt_version_id: str | None = None
    config:            dict[str, Any] = Field(default_factory=dict)
    row_count:         int   = Field(default=0, ge=0)
    row_pass_count:    int   = Field(default=0, ge=0)
    row_fail_count:    int   = Field(default=0, ge=0)
    cost_usd:          float = Field(default=0.0, ge=0)
    status:            str | None = None
    gate_status:       str | None = None
    started_at:        str | None = None
    finished_at:       str | None = None
    metrics:           dict[str, Any] = Field(default_factory=dict)
    # Hard upper bounds on nested-list cardinality so a 100 MB body
    # full of millions of nested objects can't OOM a worker after
    # passing the Content-Length cap. Defaults are an order of
    # magnitude above the largest realistic legitimate run; operators
    # who genuinely need bigger should bump in a follow-up that also
    # raises ``EVALGUARD_MAX_REQUEST_BYTES``.
    gates:             list[Gate] = Field(default_factory=list, max_length=100)
    rows:              list[Row]  = Field(default_factory=list, max_length=50_000)


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
    project:        str = Field(min_length=1, max_length=200)
    config_hash:    str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    status:         str | None = None
    row_status:     str | None = None
    gate_status:    str | None = None
    started_at:     str | None = None
    finished_at:    str | None = None
    # ``ge=0`` constraints mirror the JSON Schema's ``minimum: 0``
    # so a hostile client can't push negative counts that pass
    # Pydantic but would fail downstream validators.
    row_count:      int   = Field(default=0, ge=0)
    row_pass_count: int   = Field(default=0, ge=0)
    row_fail_count: int   = Field(default=0, ge=0)
    cost_usd:       float = Field(default=0.0, ge=0)
    # Cardinality caps mirror the per-Trial caps so the deepest nested
    # multiplication (rows × trials × gates) is bounded by request
    # construction even before SQLAlchemy gets involved.
    assets:         list[Asset] = Field(default_factory=list, max_length=10_000)
    trials:         list[Trial] = Field(default_factory=list, max_length=50)
    comparison:     Comparison | None = None
    aggregate:      Aggregate | None = None
    audit:          Audit | None = None


class RunOut(_Loose):
    """Loose response shape so old clients tolerate new fields.

    The body is what the CLI's ``run_to_dict`` produces, plus the
    server-injected ``server`` envelope (ingested_at, ingested_by,
    project_id) so an operator can see who pushed what when.

    **Trusted-input contract.** ``RunOut`` is intentionally
    ``extra='allow'`` and does **not** strip secret-shaped keys
    (``api_key``, ``password``, ``secret``, …) on read. Two reasons:

    1. The ingest path (``RunIngest``) is strict and the CLI runs
       ``redact_secrets`` over every audit payload before writing.
       Anything that reaches the storage layer either came from a
       trusted CLI or was already accepted by the ``RunIngest``
       validation contract.
    2. Stripping on read would erase fields that legitimately carry
       those names (e.g., an eval that tests a password-hashing
       function with ``password`` rows, or RAG metrics over secrets-
       detection prompts). Silent erasure on read is worse than the
       theoretical leak from a hostile-but-authenticated client.

    A regression test in ``tests/api/test_review_round_4.py``
    documents this surface and pins the behaviour.
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
    # Phase 3a: which ingest path produced this row. "cli" = pushed
    # via ``evalguard push``, "otlp" = synthesized from a posted
    # OTLP trace.  Default to "cli" for legacy rows.
    source:        str = "cli"
    ingested_at:   str | None = None
    ingested_by:   str | None = None


class RunList(_Strict):
    runs: list[RunSummary]
    next: str | None = None


# ---------------------------------------------------------------------------
# Phase OBS-1 — calls stream (one row per run_rows entry).
#
# A "call" here is one ``run_rows`` row — what the customer-service
# operator thinks of as one LLM interaction.  The endpoint that
# returns these is ``GET /v1/projects/{slug}/calls`` (newest-first
# with cursor pagination); ``CallDetail`` is the per-call drill-down
# returned by ``GET /v1/projects/{slug}/calls/{run_id}/{row_id}``.

_CALLS_TAB_DEFAULT = "recent"


class CallSummary(_Strict):
    """Stream-view card.  Reflects the denormalised columns on
    ``run_rows`` so the stream paginator never touches
    ``payload_json``.
    """

    run_id:         str
    row_id:         str
    trial_id:       str
    project_id:     str
    passed:         bool
    cost_usd:       float
    latency_ms:     int
    cache_hit:      bool
    tags:           list[str] = Field(default_factory=list)
    # Stamped by ``_persist_run`` from the parent ``runs.ingested_at``
    # so a row's wall-clock matches its run's, to the µs.  Rows
    # pre-OBS-1 have this populated by the 0007 backfill.
    ingested_at:    str | None = None
    # First 240 chars of the row's ``output``.  ``None`` for legacy
    # rows (backfill is deferred — see migration 0007 docstring) or
    # for rows that legitimately had no output (cache hits, errors).
    output_preview: str | None = None


class CallListResponse(_Strict):
    """Cursor-paginated calls list."""

    calls: list[CallSummary]
    # Opaque base64 cursor encoding ``(ingested_at, id)`` from the
    # last row of this page.  Clients pass it back as ``?cursor=``
    # for the next page.  ``None`` on the final page.
    next_cursor: str | None = None


class CallDetail(_Loose):
    """Drill-down for one call (OBS-2).

    Loose-shape (``extra='allow'``) so a future row attribute the
    server adds doesn't 422 a client that's already pinned to a
    specific Pydantic version.  The fields below are the stable
    minimum every caller relies on.

    Returned by ``GET /v1/projects/{slug}/calls/{run_id}/{row_id}``.
    Source of truth is the run's ``payload_json.trials[].rows[]``
    entry — re-parsed at request time so the heavy data stays out
    of the calls-stream paginator.
    """

    # Identifiers + context.
    run_id:        str
    row_id:        str
    trial_id:      str | None = None
    project_id:    str
    project:       str
    ingested_at:   str | None = None
    # The provider + model that produced this call (denormalised
    # from the parent trial because the UI's detail panel wants
    # them at a glance without a second fetch).
    provider:      str | None = None
    model:         str | None = None
    # Per-row outcome flags.
    passed:        bool
    n_scores:      int = 0
    cost_usd:      float = 0.0
    latency_ms:    int = 0
    cache_hit:     bool = False
    tags:          list[str] = Field(default_factory=list)
    # The "actual answer" — input/expected/output/scores.  Each
    # is optional because: (a) ``include_scores=False`` pushes
    # don't ship them, (b) some flows (cache hits, errors) have
    # no output, (c) ``expected`` is dataset-dependent.
    input:         Any = None
    expected:      Any = None
    output:        str | None = None
    scores:        list[Score] = Field(default_factory=list)
    # Trial-level gate verdicts surfaced as context.  Gate engine
    # today is trial-scoped (not row-scoped), so the UI shows
    # "the gates that ran around this row" — useful for triage
    # without claiming the gates apply per-row.
    trial_gates:   list[Gate] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Asset aggregation
#
# Runs carry ``assets[]`` rows in their JSON (one per loaded prompt /
# dataset / judge / etc).  Operators want a cross-run view: "show me
# every dataset used in this project, with version counts." This
# endpoint flattens the per-run rows into one record per
# ``(kind, asset_id)`` tuple.


class AssetSummary(_Strict):
    """One row of the aggregated assets listing."""

    kind:           str
    asset_id:       str
    project_id:     str
    project_name:   str
    version_count:  int = Field(ge=0,
        description="Distinct ``version_id`` values seen across runs.")
    run_count:      int = Field(ge=0,
        description="Distinct ``run_id`` values that referenced this asset.")
    last_seen:      str = Field(
        description="ISO timestamp of the most recent ingest of any version "
                    "of this asset (drawn from runs.ingested_at).")
    last_run_id:    str = Field(
        description="``run_id`` whose ingest produced ``last_seen``.")
    last_version_id: str = Field(
        description="``version_id`` carried on that most-recent ingest.")


class AssetList(_Strict):
    assets: list[AssetSummary]


class AssetVersionRecord(_Strict):
    """One ``(version_id, run_id)`` pair for an asset.

    The same ``version_id`` can appear in many runs (when an asset is
    reused unchanged across days of CI); we return one record per
    ingest rather than deduping so the UI can show "which run was the
    first to introduce this version" without a follow-up query.
    """

    version_id:   str
    run_id:       str
    project_name: str
    ingested_at:  str = Field(
        description="ISO timestamp from ``runs.ingested_at`` — when the asset "
                    "was first seen via this run, not the version's authoring date.")
    source:       str = Field(
        default="cli",
        description="Phase 3a — ``cli`` or ``otlp``. Mirrors the runs.source column.")


class AssetVersionsResponse(_Strict):
    """Response to ``GET /v1/assets/{kind}/{asset_id}/versions``."""

    kind:         str
    asset_id:     str
    project_id:   str
    project_name: str
    versions:     list[AssetVersionRecord]


# ---------------------------------------------------------------------------
# Drift report (Phase 3b)
#
# Compares two runs' per-row metric distributions (latency_ms,
# cost_usd, passed) via Welch's two-sample t-test. ``current`` is
# the run the operator is investigating; ``baseline`` is what they're
# comparing against. The endpoint returns one ``DriftMetric`` per
# metric so a UI can render a row-per-metric verdict table.


class DriftMetric(_Strict):
    """One metric's drift verdict."""

    name:                 str   = Field(description="latency_ms / cost_usd / passed.")
    n_current:            int   = Field(ge=2, description="Sample size on the current side.")
    n_baseline:           int   = Field(ge=2, description="Sample size on the baseline side.")
    mean_current:         float
    mean_baseline:        float
    delta_mean:           float = Field(
        description="``mean_current - mean_baseline``. Positive ⇒ current is higher.")
    t_stat:               float
    dof:                  float
    p_two_sided:          float = Field(
        description="P(observe |t| ≥ |t_stat| | no true difference). Smaller ⇒ stronger drift.")
    p_less:               float = Field(
        description="One-sided p-value for H1: current's mean < baseline's. Small ⇒ "
                    "evidence current is lower. Useful on metrics where lower is worse "
                    "(pass_rate).")
    p_greater:            float = Field(
        description="One-sided p-value for H1: current's mean > baseline's. Small ⇒ "
                    "evidence current is higher. Useful on cost / latency where higher "
                    "is worse.")
    significant_at_alpha: bool  = Field(
        description="``p_two_sided < alpha`` AND |delta_mean| > 0. The single-bit summary the UI uses.")


class DriftReport(_Strict):
    """Top-level drift response."""

    current_run_id:  str
    baseline_run_id: str
    alpha:           float = Field(gt=0, lt=1)
    metrics:         list[DriftMetric]
    skipped:         list[dict[str, Any]] = Field(
        default_factory=list,
        description="Metrics that couldn't be tested (e.g., one side had < 2 rows). "
                    "Each entry: {name, reason}.")


# ---------------------------------------------------------------------------
# Phase 4 — Argilla-style human review queue.
#
# A row that automated heuristics + judges flagged as failed (or
# borderline) goes into the queue. A reviewer picks it up, sees the
# input / output / scores, and submits one of four verdicts.
# Decisions are immutable per-reviewer (a reviewer can update their
# OWN review via UPSERT, but never overwrite someone else's), and
# multiple reviewers per row are supported so cross-annotator
# agreement can be computed later.

# Verdict enum. ``REVIEW_VERDICTS`` is the single source of truth —
# the Pydantic ``Literal`` below derives from it, and migration
# 0006_row_reviews_verdict_check pins it as a SQL CHECK constraint so
# the DB rejects bad values even if the Pydantic layer is bypassed
# (raw SQL, future ORM swap, ad-hoc backfill scripts).
REVIEW_VERDICTS: tuple[str, ...] = (
    "agree", "override_pass", "override_fail", "skip",
)
_ReviewVerdict = Literal["agree", "override_pass", "override_fail", "skip"]


class ReviewIngest(_Strict):
    """``POST /v1/reviews`` body."""

    run_id:  str = Field(pattern=r"^run_[a-z0-9]{8,}$",
                         description="The run the row belongs to.")
    row_id:  str = Field(min_length=1, max_length=200,
                         description="``row_id`` as ingested (not the surrogate ``run_rows.id``).")
    verdict: _ReviewVerdict = Field(
        description="``agree`` = automated verdict was right; "
                    "``override_pass`` = automated said fail, human says pass; "
                    "``override_fail`` = automated said pass, human says fail; "
                    "``skip`` = punt to another reviewer.")
    note:    str | None = Field(default=None, max_length=4_000,
                                description="Free-text explanation. ``None`` is allowed; "
                                            "empty string normalises to ``None`` on read.")


class ReviewOut(_Strict):
    """A single review record. Returned by ``GET /v1/runs/{id}/reviews``
    and echoed back by ``POST /v1/reviews``."""

    id:              int
    run_id:          str
    row_id:          str
    project_id:      str
    reviewer_key_id: str
    verdict:         _ReviewVerdict
    note:            str | None
    created_at:      str
    updated_at:      str


class ReviewQueueItem(_Strict):
    """One row in the queue — the row's identifying fields plus
    enough context for the reviewer to act without an extra fetch.

    Why bundle ``input`` / ``output`` / ``passed`` etc. here instead
    of asking the UI to ``getRun`` per item? A queue with 100 items
    would issue 100 follow-up fetches; bundling lets the queue page
    render at one round-trip per page.
    """

    run_id:        str
    row_id:        str
    trial_id:      str
    project_id:    str
    passed:        bool
    cost_usd:      float
    latency_ms:    int
    tags:          list[str] = Field(default_factory=list)
    # The reasons it's in the queue. Right now we surface the names
    # of the gates this row was associated with that failed; future
    # policies (judge-confidence bands, manual tagging) extend this.
    failing_gates: list[str] = Field(default_factory=list)


class ReviewQueueResponse(_Strict):
    items: list[ReviewQueueItem]
    # ``run_id`` echoed so a stale UI tab can confirm the queue it's
    # rendering matches the route it's on.
    run_id: str | None = None


class ReviewListResponse(_Strict):
    reviews: list[ReviewOut]
