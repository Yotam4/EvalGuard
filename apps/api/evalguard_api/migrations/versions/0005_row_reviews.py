"""Add ``row_reviews`` table + RLS policy.

Phase 4 — Argilla-style human review queue. One row per
(run_id, row_id, reviewer_key_id) tuple. A reviewer can update their
own review (UPSERT on the unique key) but never overwrite someone
else's.

The RLS policy mirrors the rest of the project-scoped tables: admin
sees / writes everything; a non-admin caller's ``app.org_id`` GUC
must own the row's ``project_id`` via the ``projects`` table. On
SQLite the policy DDL is skipped — the application layer is the only
enforcement point there.

Revision ID: 0005_row_reviews
Revises: 0004_rls_orgs
Create Date: 2026-05-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_row_reviews"
down_revision: Union[str, Sequence[str], None] = "0004_rls_orgs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ADMIN = "current_setting('app.is_admin', true) = '1'"
_CURRENT_ORG = "current_setting('app.org_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Same defensive pattern as 0003 — 0001 uses
    # ``metadata.create_all(checkfirst=True)`` so a fresh DB will
    # already have ``row_reviews``; older DBs that ran the original
    # 0001 do not.
    if "row_reviews" not in set(insp.get_table_names()):
        op.create_table(
            "row_reviews",
            sa.Column("id",               sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id",           sa.Text(), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
            sa.Column("row_id",           sa.Text(), nullable=False),
            sa.Column("project_id",       sa.Text(), nullable=False),
            sa.Column("reviewer_key_id",  sa.Text(), nullable=False),
            sa.Column("verdict",          sa.Text(), nullable=False),
            sa.Column("note",             sa.Text()),
            sa.Column("created_at",       sa.Text(), nullable=False),
            sa.Column("updated_at",       sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "run_id", "row_id", "reviewer_key_id",
                name="uq_row_reviews_per_reviewer",
            ),
        )
        op.create_index("idx_row_reviews_run",     "row_reviews", ["run_id"])
        op.create_index("idx_row_reviews_project", "row_reviews", ["project_id"])

    if not _is_postgres():
        return

    # RLS — mirror the ``_PROJECT_TABLES`` policy from 0002.
    op.execute("ALTER TABLE row_reviews ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE row_reviews FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY row_reviews_tenant_isolation ON row_reviews "
        f"USING ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f")) "
        f"WITH CHECK ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f"));"
    )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP POLICY IF EXISTS row_reviews_tenant_isolation ON row_reviews;")
        op.execute("ALTER TABLE row_reviews NO FORCE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE row_reviews DISABLE ROW LEVEL SECURITY;")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "row_reviews" in set(insp.get_table_names()):
        existing_indexes = {ix["name"] for ix in insp.get_indexes("row_reviews")}
        for ix in ("idx_row_reviews_run", "idx_row_reviews_project"):
            if ix in existing_indexes:
                op.drop_index(ix, table_name="row_reviews")
        op.drop_table("row_reviews")
