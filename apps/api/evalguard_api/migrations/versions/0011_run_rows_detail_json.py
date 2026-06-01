"""Add ``run_rows.detail_json`` column for live-run per-call detail.

Phase PROXY-2.  Live (proxy) runs intentionally keep
``runs.payload_json`` header-only — a live run can accumulate
millions of calls over its lifetime, and stuffing every call back into
the parent run's JSON blob would defeat the whole "stream + drill
down" architecture from Phase OBS.

Instead, the proxy persists each call's input / output / scores into
this new column on the call's own ``run_rows`` row.  Batch-ingested
runs (CLI / OTLP) leave it NULL — their per-row detail still lives in
the parent's ``payload_json`` and the OBS-2 detail endpoint already
knows how to parse it.  The endpoint prefers ``detail_json`` when
present so live calls drill down correctly and batch calls keep
working unchanged.

This is a nullable add-column on the largest table in the database;
ALTER TABLE for an additive column is metadata-only on Postgres ≥ 11
(no rewrite), so the migration is cheap even on a multi-million-row
table.  On SQLite the same ADD COLUMN is a fast schema-only change.

Revision ID: 0011_run_rows_detail_json
Revises: 0010_project_configs
Create Date: 2026-05-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011_run_rows_detail_json"
down_revision: Union[str, Sequence[str], None] = "0010_project_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("run_rows")}
    # Inspect-then-add so a fresh-DB install (where
    # ``metadata.create_all`` already produced the column) is a no-op.
    if "detail_json" not in cols:
        op.add_column(
            "run_rows",
            sa.Column("detail_json", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("run_rows")}
    if "detail_json" in cols:
        with op.batch_alter_table("run_rows") as batch:
            batch.drop_column("detail_json")
