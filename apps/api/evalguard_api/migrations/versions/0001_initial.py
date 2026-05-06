"""Initial schema — orgs, projects, api_keys, runs, trials, run_rows,
gate_results, assets, events.

Generated against ``evalguard_api.schema.metadata``. The upgrade
is a single ``metadata.create_all(bind)`` so it stays in lock-step
with the canonical declarations — no risk of a hand-edited migration
diverging from the SQLAlchemy ``Table`` objects that the runtime
queries reference by column name.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-05
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from evalguard_api.schema import metadata as target_metadata


# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    target_metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    target_metadata.drop_all(bind=bind, checkfirst=True)
