"""Re-create ``idx_run_rows_calls`` and ``idx_golden_project`` as
DESC-ordered composite indexes.

Phase OBS-1 + OBS-4 (migrations 0007 and 0008) created both indexes
with all-ASC column lists.  The corresponding queries in
``routes/calls.py:list_project_calls`` and
``routes/golden.py:list_project_golden_candidates`` order
``ORDER BY ingested_at DESC, id DESC`` / ``ORDER BY created_at DESC``
respectively.

On Postgres an ASC index for a DESC query forces a backward index
scan plus a sort step — fine on tiny tables, fatal on the 10k+ row
stream the OBS phase was designed for.  The fix is to declare the
sort columns ``DESC`` in the index itself so the planner can do a
forward seek in the right order.  SQLite ignores the ordering hint
for ``run_rows`` queries (it always plans forward + sort for our
size), but Postgres is sensitive.

Idempotent on SQLite (the recreate is a no-op when the index
already matches); the upgrade always drops + re-creates so a fresh
DB built via ``metadata.create_all`` also benefits.

Revision ID: 0009_calls_indexes_desc
Revises: 0008_golden_candidates
Create Date: 2026-05-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_calls_indexes_desc"
down_revision: Union[str, Sequence[str], None] = "0008_golden_candidates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def _recreate_index(table: str, name: str, columns: list) -> None:
    """Drop the index if present, then re-create with the requested
    column expressions.  ``columns`` may contain bare strings (ASC)
    or ``sa.text("col DESC")`` for the descending case — using
    ``sa.text`` rather than ``sa.desc(...)`` so the DDL stays
    dialect-portable (SQLite tolerates it silently)."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if name in {ix["name"] for ix in insp.get_indexes(table)}:
        op.drop_index(name, table_name=table)
    op.create_index(name, table, columns)


def upgrade() -> None:
    _recreate_index(
        "run_rows",
        "idx_run_rows_calls",
        # ``project_id`` stays ASC — it's the equality predicate.
        # ``ingested_at`` + ``id`` are the ORDER BY columns and must
        # match the query's DESC direction.
        ["project_id", sa.text("ingested_at DESC"), sa.text("id DESC")],
    )
    _recreate_index(
        "golden_candidates",
        "idx_golden_project",
        ["project_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    # Revert to the ASC shape the previous migrations declared, so
    # ``alembic downgrade`` round-trips cleanly.
    _recreate_index(
        "run_rows",
        "idx_run_rows_calls",
        ["project_id", "ingested_at", "id"],
    )
    _recreate_index(
        "golden_candidates",
        "idx_golden_project",
        ["project_id", "created_at"],
    )
