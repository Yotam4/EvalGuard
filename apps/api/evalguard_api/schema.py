"""SQLAlchemy ``MetaData`` declarations for the API server's schema.

This is the single source of truth for the database shape. Alembic's
``env.py`` imports this module, autogenerate diffs against it, and
the runtime helpers in ``db.py`` use the same column names so a
column rename in one place fails loudly at import time elsewhere.

Why SQLAlchemy core (and not the ORM)?
- Queries stay imperative and readable for the hand-written shapes
  (run ingest is a procedural fan-out, not an entity graph).
- We don't carry the lifecycle complexity of sessions / identity
  maps that the ORM brings.
- Alembic autogenerate works against ``MetaData`` directly — no ORM
  needed for migrations.

Why ``Integer`` for booleans?
- SQLite has no native ``BOOLEAN``; using ``Integer`` (with 0/1
  values) renders the same way on both backends and matches what
  the Phase-1 hand-rolled SQL already wrote.

Why ``Integer primary_key autoincrement`` for the row tables?
- SQLAlchemy renders ``INTEGER PRIMARY KEY AUTOINCREMENT`` on
  SQLite and ``BIGSERIAL PRIMARY KEY`` (or ``GENERATED ... AS
  IDENTITY``) on Postgres. Same column declaration, both work.
"""

from __future__ import annotations

from sqlalchemy import (
    Column, ForeignKey, Index, Integer, MetaData, Float,
    Table, Text, UniqueConstraint, text,
)


metadata = MetaData()


# ---------------------------------------------------------------------------
# Tenancy


orgs = Table(
    "orgs", metadata,
    Column("org_id",     Text, primary_key=True),
    Column("slug",       Text, nullable=False, unique=True),
    Column("name",       Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

projects = Table(
    "projects", metadata,
    Column("project_id", Text, primary_key=True),
    Column("org_id",     Text, ForeignKey("orgs.org_id"), nullable=False),
    Column("slug",       Text, nullable=False),
    Column("name",       Text, nullable=False),
    Column("created_at", Text, nullable=False),
    UniqueConstraint("org_id", "slug", name="uq_projects_org_slug"),
    Index("idx_projects_org", "org_id"),
)

api_keys = Table(
    "api_keys", metadata,
    Column("key_id",       Text, primary_key=True),
    Column("org_id",       Text, ForeignKey("orgs.org_id"), nullable=False),
    Column("prefix",       Text, nullable=False),
    Column("hashed_key",   Text, nullable=False, unique=True),
    Column("name",         Text, nullable=False),
    Column("scopes_csv",   Text, nullable=False, server_default=""),
    Column("created_at",   Text, nullable=False),
    Column("revoked_at",   Text),
    Column("last_used_at", Text),
    Index("idx_api_keys_org",  "org_id"),
    Index("idx_api_keys_hash", "hashed_key"),
)


# ---------------------------------------------------------------------------
# Run shape — mirrors the CLI's local schema 1:1, plus ``project_id``
# foreign keys for tenancy.


runs = Table(
    "runs", metadata,
    Column("run_id",         Text, primary_key=True),
    Column("project_id",     Text, ForeignKey("projects.project_id"), nullable=False),
    Column("project_name",   Text, nullable=False),
    Column("config_hash",    Text),
    Column("status",         Text),
    Column("row_status",     Text),
    Column("gate_status",    Text),
    Column("started_at",     Text),
    Column("finished_at",    Text),
    Column("cost_usd",       Float, server_default="0"),
    Column("row_count",      Integer, server_default="0"),
    Column("row_pass_count", Integer, server_default="0"),
    Column("row_fail_count", Integer, server_default="0"),
    Column("payload_json",   Text, nullable=False),
    Column("ingested_at",    Text, nullable=False),
    Column("ingested_by",    Text),
    # 'cli' for runs pushed via ``evalguard push``; 'otlp' for runs
    # synthesized from OpenTelemetry GenAI traces. Used by the UI
    # to surface a source badge without joining into payload_json.
    Column("source",         Text, nullable=False, server_default="cli"),
    Index("idx_runs_project", "project_id", "ingested_at"),
    Index("idx_runs_status",  "status"),
    Index("idx_runs_source",  "source"),
)

trials = Table(
    "trials", metadata,
    Column("trial_id",          Text, primary_key=True),
    Column("run_id",            Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    Column("project_id",        Text, nullable=False),
    Column("provider_id",       Text),
    Column("provider",          Text),
    Column("model",             Text),
    Column("prompt_id",         Text),
    Column("prompt_version_id", Text),
    Column("row_count",         Integer, server_default="0"),
    Column("row_pass_count",    Integer, server_default="0"),
    Column("row_fail_count",    Integer, server_default="0"),
    Column("cost_usd",          Float, server_default="0"),
    Column("status",            Text),
    Column("gate_status",       Text),
    Column("started_at",        Text),
    Column("finished_at",       Text),
    Index("idx_trials_run", "run_id"),
)

run_rows = Table(
    "run_rows", metadata,
    Column("id",             Integer, primary_key=True, autoincrement=True),
    Column("run_id",         Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    Column("trial_id",       Text, nullable=False),
    Column("project_id",     Text, nullable=False),
    Column("row_id",         Text, nullable=False),
    Column("passed",         Integer, nullable=False),
    Column("n_scores",       Integer, nullable=False),
    Column("cost_usd",       Float, server_default="0"),
    Column("latency_ms",     Integer, server_default="0"),
    Column("cache_hit",      Integer, server_default="0"),
    Column("tags_json",      Text),
    # Phase OBS-1: denormalised ``runs.ingested_at`` so the
    # project-wide ``calls`` stream can paginate over a single
    # composite index without a JOIN.  Stamped at insert in
    # ``_persist_run``; backfilled by migration 0007 for older rows.
    Column("ingested_at",    Text),
    # First ~240 chars of ``rows[].output`` so the stream UI's row
    # card can show a preview without rehydrating ``payload_json``.
    # Nullable because (a) backfill leaves older rows blank and
    # (b) some rows legitimately have no output (cache hits, errors).
    Column("output_preview", Text),
    # Phase PROXY-2: per-row detail blob for live (proxy) calls.
    # Batch-ingested runs leave this NULL — their per-row detail still
    # lives in ``runs.payload_json`` and is parsed on demand by the
    # OBS-2 detail endpoint.  Live runs do the opposite: their parent
    # ``runs.payload_json`` is header-only (no inline rows, by design,
    # so a million-call live run doesn't materialise as one giant
    # JSON blob), and the per-call input / output / scores live here.
    # The detail endpoint prefers ``detail_json`` when present.
    Column("detail_json",    Text),
    Index("idx_run_rows_run",   "run_id"),
    Index("idx_run_rows_trial", "trial_id"),
    # Composite index that drives the calls stream's two tabs:
    # ``recent``  ⇒ ``ORDER BY ingested_at DESC, id DESC``
    # ``failures`` ⇒ ``WHERE passed = 0 ORDER BY ingested_at DESC, id DESC``
    # Index column order matches the WHERE+ORDER pair so both
    # planners pick it without a sort step.  The ORDER BY columns
    # are declared DESC so Postgres can do a forward seek (an ASC
    # index for a DESC query forces a backward scan + sort).  SQLite
    # ignores the directionality at our table sizes.
    Index("idx_run_rows_calls",
          "project_id", text("ingested_at DESC"), text("id DESC")),
)

gate_results = Table(
    "gate_results", metadata,
    Column("id",           Integer, primary_key=True, autoincrement=True),
    Column("run_id",       Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    Column("trial_id",     Text),
    Column("project_id",   Text, nullable=False),
    Column("gate_name",    Text, nullable=False),
    Column("blocking",     Integer),
    Column("passed",       Integer, nullable=False),
    Column("severity",     Text),
    Column("layer",        Integer),
    Column("details_json", Text),
    Index("idx_gate_run", "run_id"),
)

assets = Table(
    "assets", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("run_id",     Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    Column("project_id", Text, nullable=False),
    Column("kind",       Text, nullable=False),
    Column("asset_id",   Text, nullable=False),
    Column("version_id", Text, nullable=False),
    Column("source",     Text),
    Index("idx_assets_run", "run_id"),
)

events = Table(
    "events", metadata,
    Column("id",          Integer, primary_key=True, autoincrement=True),
    Column("run_id",      Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("project_id",  Text, nullable=False),
    Column("event_count", Integer, nullable=False),
    Column("chain_tip",   Text),
    Column("events_json", Text, nullable=False),
)


# Phase OBS-4 — golden-candidates staging table.
#
# When a reviewer (or anyone with access to a call's detail page)
# decides a row is worth keeping as a regression case, they click
# "Promote to golden".  That POSTs to ``/v1/golden/candidates`` and
# lands a row here.  A separate (future) CLI command exports the
# staged candidates to the operator's on-disk JSONL dataset — this
# table is the staging area between "I noticed this in production"
# and "this is now part of my golden dataset".
#
# UNIQUE(run_id, row_id, promoted_by) means a reviewer can re-click
# Promote idempotently (no duplicate row) but two different reviewers
# can independently promote the same row (their notes captured
# separately).
golden_candidates = Table(
    "golden_candidates", metadata,
    Column("id",          Integer, primary_key=True, autoincrement=True),
    Column("run_id",      Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    Column("row_id",      Text, nullable=False),
    Column("project_id",  Text, nullable=False),
    Column("promoted_by", Text, nullable=False),
    Column("note",        Text),
    Column("created_at",  Text, nullable=False),
    UniqueConstraint("run_id", "row_id", "promoted_by",
                     name="uq_golden_candidates_per_reviewer"),
    # ``created_at DESC`` because the list endpoint orders newest-
    # first; same Postgres backward-scan concern as ``run_rows``.
    Index("idx_golden_project", "project_id", text("created_at DESC")),
)


# Phase 4 — Argilla-style human review queue. One row per
# (run_id, row_id, reviewer_key_id) tuple. A reviewer can update
# their own review (UPSERT on the unique key) but never overwrite
# someone else's. ``project_id`` is denormalised onto each review so
# RLS scopes by org without an extra join through ``run_rows``.
row_reviews = Table(
    "row_reviews", metadata,
    Column("id",               Integer, primary_key=True, autoincrement=True),
    Column("run_id",           Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    Column("row_id",           Text, nullable=False),
    Column("project_id",       Text, nullable=False),
    Column("reviewer_key_id",  Text, nullable=False),
    # 'agree'         — the automated verdict (pass/fail) was right
    # 'override_pass' — automated said fail, human says pass
    # 'override_fail' — automated said pass, human says fail
    # 'skip'          — reviewer punts; row stays in the queue for
    #                   the NEXT reviewer (different key_id) to pick
    Column("verdict",          Text, nullable=False),
    Column("note",             Text),
    Column("created_at",       Text, nullable=False),
    Column("updated_at",       Text, nullable=False),
    UniqueConstraint("run_id", "row_id", "reviewer_key_id",
                     name="uq_row_reviews_per_reviewer"),
    Index("idx_row_reviews_run",     "run_id"),
    Index("idx_row_reviews_project", "project_id"),
)


# Phase PROXY-1 — server-side project config storage. Each row is one
# uploaded ``evalguard.yaml`` blob, addressable by its SHA-256
# content hash. ``UNIQUE (project_id, content_sha256)`` makes re-pushing
# the same bytes idempotent (the POST endpoint returns the existing
# record instead of inserting a duplicate). The latest config for a
# project is ``ORDER BY pushed_at DESC, id DESC LIMIT 1`` — the index
# is shaped for that read.
project_configs = Table(
    "project_configs", metadata,
    Column("id",             Integer, primary_key=True, autoincrement=True),
    Column("project_id",     Text,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("content",        Text, nullable=False),
    Column("pushed_by",      Text, nullable=False),
    Column("pushed_at",      Text, nullable=False),
    UniqueConstraint("project_id", "content_sha256",
                     name="uq_project_configs_content"),
    Index("idx_project_configs_latest",
          "project_id", text("pushed_at DESC"), text("id DESC")),
)


# Tables that carry a ``project_id`` and whose rows are subject to
# tenant scoping. The RLS migration enables RLS + creates policies on
# this exact list, and any future table that touches per-org data
# should be added here so RLS coverage doesn't drift.
RLS_TARGET_TABLES: tuple[str, ...] = (
    "projects",
    "api_keys",
    "runs",
    "trials",
    "run_rows",
    "gate_results",
    "assets",
    "events",
    "row_reviews",
    "golden_candidates",
    "project_configs",
    "event_rows",
)


# Phase PROXY-3.5 — per-event audit chain rows.
#
# The existing ``events`` table is one-blob-per-run (UNIQUE on
# run_id, the whole event chain serialised into ``events_json``).
# That shape was correct for CLI/OTLP batch ingest: a run completes,
# its full audit chain arrives in one POST, gets stored as one blob.
#
# The proxy can't fit that shape — a live run accumulates events
# across many distinct ``/invoke`` calls over a day.  Re-serialising
# the whole blob per call would be O(N²) in events and re-writing it
# atomically against concurrent writers is awkward.
#
# ``event_rows`` is the per-event granular form: one row per
# emitted event, chain-linked via ``prev_event_hash``.
# ``UNIQUE (run_id, prev_event_hash)`` is the linchpin: only ONE
# event can ever follow a given chain tip, so two concurrent writers
# racing on the same run see one win and the other catches an
# IntegrityError and retries with the fresh tip.  Without that
# constraint a fork would silently corrupt the chain.
#
# ``event_json`` carries the full canonical event dict — same shape
# ``build_event`` returns — so ``verify_chain_events`` can re-hash
# verbatim without us having to perfectly denormalise every PROV
# field into a separate column.  Disk space is cheap; audit clarity
# is not.
event_rows = Table(
    "event_rows", metadata,
    Column("id",              Integer, primary_key=True, autoincrement=True),
    Column("event_id",        Text, nullable=False, unique=True),
    Column("run_id",          Text,
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False),
    Column("trial_id",        Text),
    Column("row_id",          Text),
    Column("project_id",      Text, nullable=False),
    Column("kind",            Text, nullable=False),
    Column("actor_id",        Text, nullable=False),
    Column("actor_type",      Text, nullable=False),
    Column("subject_kind",    Text),
    Column("subject_id",      Text),
    Column("cost_usd",        Float),
    Column("duration_ms",     Integer),
    Column("prev_event_hash", Text),
    Column("event_hash",      Text, nullable=False),
    Column("event_json",      Text, nullable=False),
    Column("ingested_at",     Text, nullable=False),
    UniqueConstraint("run_id", "prev_event_hash",
                     name="uq_event_rows_chain"),
    Index("idx_event_rows_run",  "run_id", "id"),
    Index("idx_event_rows_proj", "project_id", "ingested_at", "id"),
)
