"""Row-Level Security on ``orgs`` (Postgres only).

Migration 0002 deliberately skipped ``orgs`` because the table doesn't
carry an ``org_id`` column (it IS the orgs table) — the application
layer enforced "members see only their own org" via
``filter_orgs_visible_to``. The Phase-A round-3 review flagged that
gap as defense-in-depth missing on the most sensitive enumeration
target: any future code path that queries ``orgs`` directly (a debug
endpoint, a SQL injection, a refactor that bypasses
``filter_orgs_visible_to``) would expose tenant org enumeration to
any authenticated caller.

This migration closes the gap: ``orgs.org_id`` is the row's *own*
identifier, so the policy compares it against
``current_setting('app.org_id', true)`` directly. Admins still see
all orgs (their bootstrap is the only way to create new ones); a
non-admin caller sees exactly one row — the org their API key
belongs to.

Revision ID: 0004_rls_orgs
Revises: 0003_runs_source
Create Date: 2026-05-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0004_rls_orgs"
down_revision: Union[str, Sequence[str], None] = "0003_runs_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ADMIN = "current_setting('app.is_admin', true) = '1'"
_CURRENT_ORG = "current_setting('app.org_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute("ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE orgs FORCE ROW LEVEL SECURITY;")
    op.execute(
        # Note: ``orgs.org_id`` is the row's own primary key here, not
        # a foreign-key column like in 0002's ``projects``/``api_keys``
        # policies. The semantics ("a non-admin sees only their own
        # org") is therefore enforced by comparing the row's identity
        # against the caller's org GUC.
        f"CREATE POLICY orgs_tenant_isolation ON orgs "
        f"USING ({_ADMIN} OR org_id = {_CURRENT_ORG}) "
        f"WITH CHECK ({_ADMIN} OR org_id = {_CURRENT_ORG});"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("DROP POLICY IF EXISTS orgs_tenant_isolation ON orgs;")
    op.execute("ALTER TABLE orgs NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE orgs DISABLE ROW LEVEL SECURITY;")
