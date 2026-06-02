"""Add ``event_rows`` table + RLS policy — per-event audit chain.

Phase PROXY-3.5.  The existing ``events`` table is one-blob-per-run
(UNIQUE on run_id, the full chain in ``events_json``); it was
correct for CLI/OTLP batch ingest where a run completes and arrives
as a single POST.  The proxy can't fit that shape — a live run
accumulates events across many distinct ``/invoke`` calls.

``event_rows`` is the per-event granular form.  ``UNIQUE (run_id,
prev_event_hash)`` is the chain-fork-prevention linchpin: only ONE
event can follow a given chain tip per run, so concurrent writers
that race on the same chain tip see one winner and the loser
catches an IntegrityError and retries with the fresh tip.  The full
canonical event dict lives in ``event_json`` so the
``verify_chain_events`` helper can re-hash verbatim.

RLS policy matches the project-scoped pattern from 0008
(golden_candidates) and 0010 (project_configs).

Revision ID: 0012_event_rows
Revises: 0011_run_rows_detail_json
Create Date: 2026-06-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_event_rows"
down_revision: Union[str, Sequence[str], None] = "0011_run_rows_detail_json"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


_ADMIN = "current_setting('app.is_admin', true) = '1'"
_CURRENT_ORG = "current_setting('app.org_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "event_rows" not in set(insp.get_table_names()):
        op.create_table(
            "event_rows",
            sa.Column("id",              sa.Integer(),
                primary_key=True, autoincrement=True),
            sa.Column("event_id",        sa.Text(), nullable=False, unique=True),
            sa.Column("run_id",          sa.Text(),
                sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
                nullable=False),
            sa.Column("trial_id",        sa.Text()),
            sa.Column("row_id",          sa.Text()),
            sa.Column("project_id",      sa.Text(), nullable=False),
            sa.Column("kind",            sa.Text(), nullable=False),
            sa.Column("actor_id",        sa.Text(), nullable=False),
            sa.Column("actor_type",      sa.Text(), nullable=False),
            sa.Column("subject_kind",    sa.Text()),
            sa.Column("subject_id",      sa.Text()),
            sa.Column("cost_usd",        sa.Float()),
            sa.Column("duration_ms",     sa.Integer()),
            sa.Column("prev_event_hash", sa.Text()),
            sa.Column("event_hash",      sa.Text(), nullable=False),
            sa.Column("event_json",      sa.Text(), nullable=False),
            sa.Column("ingested_at",     sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "run_id", "prev_event_hash",
                name="uq_event_rows_chain",
            ),
        )
        op.create_index(
            "idx_event_rows_run",
            "event_rows", ["run_id", "id"],
        )
        op.create_index(
            "idx_event_rows_proj",
            "event_rows", ["project_id", "ingested_at", "id"],
        )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE event_rows ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE event_rows FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY event_rows_tenant_isolation ON event_rows "
        f"USING ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f")) "
        f"WITH CHECK ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f"));"
    )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            "DROP POLICY IF EXISTS event_rows_tenant_isolation ON event_rows;"
        )
        op.execute("ALTER TABLE event_rows NO FORCE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE event_rows DISABLE ROW LEVEL SECURITY;")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "event_rows" in set(insp.get_table_names()):
        existing_indexes = {ix["name"] for ix in insp.get_indexes("event_rows")}
        for idx in ("idx_event_rows_proj", "idx_event_rows_run"):
            if idx in existing_indexes:
                op.drop_index(idx, table_name="event_rows")
        op.drop_table("event_rows")
