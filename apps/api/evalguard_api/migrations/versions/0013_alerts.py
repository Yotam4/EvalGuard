"""Add ``alerts`` + ``alert_state`` tables — rolling-window alerting.

Phase PROXY-3 Slice C.  The proxy's per-call pass-rate is already
queryable via ``/v1/projects/{slug}/live/aggregate`` and the per-row
audit chain captures every block / timeout; what's missing is a
mechanism that **fires on its own** when a rolling-window aggregate
crosses a threshold (e.g. "pass rate over the last 15 minutes drops
below 0.9").

Two tables:

- ``alerts`` — append-only history of every fired alert.  Drives
  the ``GET /v1/projects/{slug}/alerts`` history view and the audit
  trail (each row also gets an ``alert.fired`` event in the
  project chain).
- ``alert_state`` — one row per ``(project_id, rule_id)`` holding
  the current dedup state.  The cron-driven evaluator reads this
  to decide whether to re-fire on a still-failing rule (``state =
  fail`` + ``last_fire_at`` within ``suppress_secs``: skip) or
  emit a resolution (``state = fail`` → window now passes:
  transition to ``pass`` and emit ``alert.resolved``).

Both tables use the same project-scoped RLS pattern as 0008 /
0010 / 0012.  No ``ON DELETE CASCADE`` from projects — alerts
outlive the projects they describe, mirroring the audit chain's
SOC 2-style retention posture.

Revision ID: 0013_alerts
Revises: 0012_event_rows
Create Date: 2026-06-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013_alerts"
down_revision: Union[str, Sequence[str], None] = "0012_event_rows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


_ADMIN = "current_setting('app.is_admin', true) = '1'"
_CURRENT_ORG = "current_setting('app.org_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    if "alerts" not in existing:
        op.create_table(
            "alerts",
            sa.Column("id",             sa.Integer(),
                primary_key=True, autoincrement=True),
            sa.Column("project_id",     sa.Text(), nullable=False),
            sa.Column("rule_id",        sa.Text(), nullable=False),
            sa.Column("fired_at",       sa.Text(), nullable=False),
            sa.Column("window_start",   sa.Text(), nullable=False),
            sa.Column("window_end",     sa.Text(), nullable=False),
            sa.Column("gate",           sa.Text(), nullable=False),
            sa.Column("observed_value", sa.Float()),
            sa.Column("threshold_json", sa.Text()),
            sa.Column("transition",     sa.Text(), nullable=False),
            sa.Column("suppressed",     sa.Integer(), nullable=False, server_default="0"),
            sa.Column("notify_results_json", sa.Text()),
        )
        op.create_index(
            "idx_alerts_project_fired",
            "alerts", ["project_id", "fired_at", "id"],
        )

    if "alert_state" not in existing:
        op.create_table(
            "alert_state",
            sa.Column("project_id",         sa.Text(), nullable=False),
            sa.Column("rule_id",            sa.Text(), nullable=False),
            sa.Column("state",              sa.Text(), nullable=False,
                server_default="pass"),
            sa.Column("last_transition_at", sa.Text()),
            sa.Column("last_fire_at",       sa.Text()),
            sa.Column("last_check_at",      sa.Text()),
            sa.PrimaryKeyConstraint("project_id", "rule_id",
                                    name="pk_alert_state"),
        )

    if not _is_postgres():
        return

    op.execute("ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE alerts FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY alerts_tenant_isolation ON alerts "
        f"USING ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f")) "
        f"WITH CHECK ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f"));"
    )

    op.execute("ALTER TABLE alert_state ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE alert_state FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY alert_state_tenant_isolation ON alert_state "
        f"USING ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f")) "
        f"WITH CHECK ({_ADMIN} OR project_id IN ("
        f"  SELECT project_id FROM projects WHERE org_id = {_CURRENT_ORG}"
        f"));"
    )


def downgrade() -> None:
    if _is_postgres():
        for tbl, pol in (
            ("alerts",      "alerts_tenant_isolation"),
            ("alert_state", "alert_state_tenant_isolation"),
        ):
            op.execute(f"DROP POLICY IF EXISTS {pol} ON {tbl};")
            op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY;")
            op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY;")

    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if "alerts" in existing:
        idxs = {ix["name"] for ix in insp.get_indexes("alerts")}
        if "idx_alerts_project_fired" in idxs:
            op.drop_index("idx_alerts_project_fired", table_name="alerts")
        op.drop_table("alerts")
    if "alert_state" in existing:
        op.drop_table("alert_state")
