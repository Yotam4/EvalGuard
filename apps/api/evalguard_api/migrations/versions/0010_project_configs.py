"""Add ``project_configs`` table + RLS policy.

Phase PROXY-1.  Server-side storage for per-project ``evalguard.yaml``
blobs so the upcoming ``POST /v1/projects/{slug}/invoke`` proxy can
resolve config without trusting the caller to supply it.  Pushed via
``evalguard push-config``; each upload is content-addressed by
SHA-256 so re-pushing identical bytes is a no-op (UPSERT returns the
existing row).

RLS policy mirrors the rest of the project-scoped tables (see 0008
golden_candidates for the same shape): admin sees everything; a non-
admin caller's ``app.org_id`` GUC must own the row's ``project_id``
via the ``projects`` table.  Table-and-policy ship together in the
same migration so the RLS-coverage introspection test
(``test_postgres_rls_policies_present_on_every_target_table``) goes
green the moment the migration runs.

Revision ID: 0010_project_configs
Revises: 0009_calls_indexes_desc
Create Date: 2026-05-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_project_configs"
down_revision: Union[str, Sequence[str], None] = "0009_calls_indexes_desc"
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
    if "project_configs" not in set(insp.get_table_names()):
        op.create_table(
            "project_configs",
            sa.Column("id",             sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("project_id",     sa.Text(),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=False),
            sa.Column("content_sha256", sa.Text(), nullable=False),
            sa.Column("content",        sa.Text(), nullable=False),
            sa.Column("pushed_by",      sa.Text(), nullable=False),
            sa.Column("pushed_at",      sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "project_id", "content_sha256",
                name="uq_project_configs_content",
            ),
        )
        # Postgres honours the DESC directive in the index; SQLite is
        # tolerant of the syntax (parses as ASC) at current scale.
        if _is_postgres():
            op.execute(
                "CREATE INDEX idx_project_configs_latest "
                "ON project_configs (project_id, pushed_at DESC, id DESC)"
            )
        else:
            op.create_index(
                "idx_project_configs_latest",
                "project_configs",
                ["project_id", "pushed_at", "id"],
            )

    if not _is_postgres():
        return

    # RLS — mirror the ``_PROJECT_TABLES`` policy from 0002.
    op.execute("ALTER TABLE project_configs ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE project_configs FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY project_configs_tenant_isolation ON project_configs "
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
            "DROP POLICY IF EXISTS project_configs_tenant_isolation ON project_configs;"
        )
        op.execute("ALTER TABLE project_configs NO FORCE ROW LEVEL SECURITY;")
        op.execute("ALTER TABLE project_configs DISABLE ROW LEVEL SECURITY;")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "project_configs" in set(insp.get_table_names()):
        existing_indexes = {ix["name"] for ix in insp.get_indexes("project_configs")}
        if "idx_project_configs_latest" in existing_indexes:
            op.drop_index("idx_project_configs_latest", table_name="project_configs")
        op.drop_table("project_configs")
