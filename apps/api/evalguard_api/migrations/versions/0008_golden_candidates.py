"""Add ``golden_candidates`` staging table + RLS policy.

Phase OBS-4.  When a reviewer (or anyone with access to a call's
detail page) decides a row is worth keeping as a regression case,
they click "Promote to golden" — that POSTs to
``/v1/golden/candidates`` and lands here.

UNIQUE(run_id, row_id, promoted_by) means a reviewer can re-click
Promote idempotently (no duplicate row) but two different reviewers
can independently promote the same row.  Same composite-key pattern
as ``row_reviews`` (0005).

RLS policy mirrors the rest of ``_PROJECT_TABLES``: admin sees /
writes everything; a non-admin caller's ``app.org_id`` GUC must own
the row's ``project_id`` via the ``projects`` table.  On SQLite the
policy DDL is skipped — the application layer is the only
enforcement point there.

Revision ID: 0008_golden_candidates
Revises: 0007_run_rows_calls_index
Create Date: 2026-05-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_golden_candidates"
down_revision: Union[str, Sequence[str], None] = "0007_run_rows_calls_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


_ADMIN = "current_setting('app.is_admin', true) = '1'"
_CURRENT_ORG = "current_setting('app.org_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Inspect-then-create so a fresh-DB install (where
    # ``metadata.create_all`` already produced the table from the
    # current MetaData) is a no-op.
    if "golden_candidates" not in set(insp.get_table_names()):
        op.create_table(
            "golden_candidates",
            sa.Column("id",          sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id",      sa.Text(),
                sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
                nullable=False),
            sa.Column("row_id",      sa.Text(), nullable=False),
            sa.Column("project_id",  sa.Text(), nullable=False),
            sa.Column("promoted_by", sa.Text(), nullable=False),
            sa.Column("note",        sa.Text()),
            sa.Column("created_at",  sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "run_id", "row_id", "promoted_by",
                name="uq_golden_candidates_per_reviewer",
            ),
        )
        op.create_index(
            "idx_golden_project",
            "golden_candidates",
            ["project_id", "created_at"],
        )

    if not _is_postgres():
        return

    # RLS — mirror the ``_PROJECT_TABLES`` policy from 0002.
    op.execute("ALTER TABLE golden_candidates ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE golden_candidates FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY golden_candidates_tenant_isolation ON golden_candidates "
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
            "DROP POLICY IF EXISTS golden_candidates_tenant_isolation ON golden_candidates;"
        )
        op.execute("ALTER TABLE golden_candidates NO FORCE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE golden_candidates DISABLE ROW LEVEL SECURITY;")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "golden_candidates" in set(insp.get_table_names()):
        existing_indexes = {ix["name"] for ix in insp.get_indexes("golden_candidates")}
        if "idx_golden_project" in existing_indexes:
            op.drop_index("idx_golden_project", table_name="golden_candidates")
        op.drop_table("golden_candidates")
