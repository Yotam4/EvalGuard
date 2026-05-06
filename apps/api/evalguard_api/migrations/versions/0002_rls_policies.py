"""Row-Level Security policies (Postgres only).

Defense-in-depth on top of the application-layer auth in
``evalguard_api/auth.py``. With these policies enabled, every
SELECT / INSERT / UPDATE / DELETE that the server issues is
filtered by Postgres itself based on two session-local GUCs:

- ``app.org_id`` — set per request to the caller's ``Principal.org_id``
- ``app.is_admin`` — ``"1"`` for admin scope, ``"0"`` otherwise

The runtime sets both via ``set_config(..., is_local=true)`` inside
``deps.get_conn`` at the start of every transaction (see
``evalguard_api/db.py:apply_rls_context``). On SQLite this migration
is a no-op — SQLite has no RLS, so the policies are skipped and the
application layer is the only enforcement point.

Rationale for the policy expressions:

- Admin always sees / can write everything (the bootstrap admin key
  is the only way to create new orgs, so cross-org access is by
  design for admins).
- For non-admin callers, the row's ``org_id`` (or its
  ``project_id``'s owning org) must equal ``app.org_id``.
- ``api_keys`` rows: row's ``org_id`` must match.
- Run-shape rows (runs / trials / run_rows / gate_results / assets /
  events): the row's ``project_id`` must belong to the caller's org
  — checked via a correlated subquery against ``projects``.

Why not on ``orgs`` itself? The ``orgs`` table doesn't carry an
``org_id`` column (it IS the orgs table). The application layer
enforces "members see only their own org" via
``filter_orgs_visible_to``. Adding RLS to ``orgs`` would require a
self-referencing policy that doesn't add information beyond what
the application already enforces.

Revision ID: 0002_rls_policies
Revises: 0001_initial
Create Date: 2026-05-05
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0002_rls_policies"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables whose rows carry an ``org_id`` directly.
_ORG_TABLES = ("projects", "api_keys")

# Tables whose rows carry a ``project_id`` (and therefore an org_id
# transitively via the projects table).
_PROJECT_TABLES = ("runs", "trials", "run_rows", "gate_results", "assets", "events")


def _is_postgres() -> bool:
    """Skip RLS DDL on SQLite — it doesn't support it. SQLite-only
    deployments rely entirely on the application-layer auth."""
    return op.get_bind().dialect.name == "postgresql"


# Policy SQL fragments. ``current_setting('app.is_admin', true) = '1'``
# returns NULL when the GUC isn't set — combined with ``= '1'`` that
# reads as false, so a forgotten ``apply_rls_context`` call defaults
# to non-admin enforcement (fail-closed).
_ADMIN = "current_setting('app.is_admin', true) = '1'"
_CURRENT_ORG = "current_setting('app.org_id', true)"


def upgrade() -> None:
    if not _is_postgres():
        return

    for tbl in _ORG_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {tbl}_tenant_isolation ON {tbl} "
            f"USING ({_ADMIN} OR org_id = {_CURRENT_ORG}) "
            f"WITH CHECK ({_ADMIN} OR org_id = {_CURRENT_ORG});"
        )

    for tbl in _PROJECT_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {tbl}_tenant_isolation ON {tbl} "
            f"USING ({_ADMIN} OR project_id IN ("
            f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
            f")) "
            f"WITH CHECK ({_ADMIN} OR project_id IN ("
            f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
            f"));"
        )


def downgrade() -> None:
    if not _is_postgres():
        return

    for tbl in _ORG_TABLES + _PROJECT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl};")
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY;")
