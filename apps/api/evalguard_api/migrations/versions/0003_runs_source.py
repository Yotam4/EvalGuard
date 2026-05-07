"""Add ``runs.source`` column.

Phase 3a introduces a second ingest path (OTLP traces) alongside
the Phase-2 ``evalguard push`` flow.  The UI / list endpoints need
to distinguish the two cheaply, so a dedicated column beats parsing
``payload_json`` on every list query.

Existing rows backfill to ``'cli'`` — that's what they were when
the table only had one ingest path.

Revision ID: 0003_runs_source
Revises: 0002_rls_policies
Create Date: 2026-05-08
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_runs_source"
down_revision: Union[str, Sequence[str], None] = "0002_rls_policies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0001_initial uses ``metadata.create_all(checkfirst=True)``,
    # which means a fresh DB will already have the ``source``
    # column when 0003 runs (the current MetaData reflects today's
    # schema). The pre-2.6c databases that ran the original 0001
    # do NOT have it. Inspect first so the migration is a no-op on
    # already-current DBs and a real ALTER on the older ones.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("runs")}
    if "source" not in cols:
        # ``batch_alter_table`` is the one-knob way to make ALTER
        # TABLE work on SQLite (which historically couldn't add
        # NOT-NULL-with-default columns). On Postgres it's a normal
        # ALTER and the batch wrapper is a no-op.
        with op.batch_alter_table("runs") as batch:
            batch.add_column(
                sa.Column(
                    "source",
                    sa.Text(),
                    nullable=False,
                    server_default="cli",
                ),
            )

    existing_indexes = {ix["name"] for ix in insp.get_indexes("runs")}
    if "idx_runs_source" not in existing_indexes:
        op.create_index("idx_runs_source", "runs", ["source"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in insp.get_indexes("runs")}
    if "idx_runs_source" in existing_indexes:
        op.drop_index("idx_runs_source", table_name="runs")
    cols = {c["name"] for c in insp.get_columns("runs")}
    if "source" in cols:
        with op.batch_alter_table("runs") as batch:
            batch.drop_column("source")
