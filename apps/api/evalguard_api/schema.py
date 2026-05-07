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
    Table, Text, UniqueConstraint,
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
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("run_id",     Text, ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
    Column("trial_id",   Text, nullable=False),
    Column("project_id", Text, nullable=False),
    Column("row_id",     Text, nullable=False),
    Column("passed",     Integer, nullable=False),
    Column("n_scores",   Integer, nullable=False),
    Column("cost_usd",   Float, server_default="0"),
    Column("latency_ms", Integer, server_default="0"),
    Column("cache_hit",  Integer, server_default="0"),
    Column("tags_json",  Text),
    Index("idx_run_rows_run",   "run_id"),
    Index("idx_run_rows_trial", "trial_id"),
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
)
