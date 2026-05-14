"""Add CHECK constraint on ``row_reviews.verdict``.

Round-5 review caught that the verdict enum
(``agree`` / ``override_pass`` / ``override_fail`` / ``skip``) was
only enforced in Pydantic — a raw SQL insert or a future ORM layer
that bypasses ``ReviewIngest`` could store arbitrary strings. The
state-machine lived in Python alone.

This migration locks it at the storage layer. SQLite supports CHECK
constraints inline; Postgres needs the same syntax. Both honour the
literal-set form below.

Revision ID: 0006_row_reviews_verdict_check
Revises: 0005_row_reviews
Create Date: 2026-05-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_row_reviews_verdict_check"
down_revision: Union[str, Sequence[str], None] = "0005_row_reviews"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


# Mirrors ``apps/api/evalguard_api/models.REVIEW_VERDICTS`` exactly.
# Drift between the two would let the DB accept verdicts the API
# rejects (or vice versa). A drift test pins the pair in
# ``tests/api/test_review_round_5.py``.
_VERDICTS: tuple[str, ...] = (
    "agree", "override_pass", "override_fail", "skip",
)


def _check_expr() -> str:
    quoted = ", ".join(f"'{v}'" for v in _VERDICTS)
    return f"verdict IN ({quoted})"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Cleanup pass: any existing rows with verdicts outside the
    # enum (e.g. from a buggy direct INSERT during development) get
    # parked as ``skip`` so the constraint application doesn't fail.
    # In a production deployment the row_reviews table is brand new
    # (0005 created it), so this is defensive — the DELETE/UPDATE
    # is a no-op when every row is already compliant.
    quoted = ", ".join(f"'{v}'" for v in _VERDICTS)
    op.execute(
        f"UPDATE row_reviews SET verdict = 'skip' "
        f"WHERE verdict NOT IN ({quoted})"
    )

    if dialect == "sqlite":
        # SQLite cannot ALTER TABLE ADD CONSTRAINT. The portable
        # path is ``batch_alter_table`` which transparently does the
        # table-recreate dance. On a brand-new install (where 0005
        # just created the table) this is one round-trip + one rename.
        with op.batch_alter_table("row_reviews", recreate="always") as batch:
            batch.create_check_constraint(
                "ck_row_reviews_verdict", _check_expr(),
            )
            # Tighten ``row_id`` from TEXT to VARCHAR(200) so the
            # ``max_length=200`` constraint in ``ReviewIngest.row_id``
            # is enforced at the storage layer too. SQLite ignores
            # VARCHAR length internally but the schema metadata
            # round-trips through SQLAlchemy reflection cleanly.
            batch.alter_column(
                "row_id",
                existing_type=sa.Text(),
                type_=sa.String(200),
                existing_nullable=False,
            )
        return

    # Postgres: ALTER TABLE ADD CONSTRAINT … NOT VALID first so we
    # don't lock the table while scanning existing rows, then VALIDATE
    # in a second statement (cheap on the brand-new table; safe to
    # run on a populated table too because the cleanup pass above
    # made every row compliant).
    op.execute(
        f"ALTER TABLE row_reviews "
        f"ADD CONSTRAINT ck_row_reviews_verdict "
        f"CHECK ({_check_expr()}) NOT VALID;"
    )
    op.execute(
        "ALTER TABLE row_reviews VALIDATE CONSTRAINT ck_row_reviews_verdict;"
    )
    # Tighten the column type. Postgres accepts ALTER COLUMN TYPE
    # with USING when the cast is unambiguous; TEXT → VARCHAR(200)
    # is unambiguous as long as no existing value exceeds 200 chars.
    op.alter_column(
        "row_reviews",
        "row_id",
        existing_type=sa.Text(),
        type_=sa.String(200),
        existing_nullable=False,
    )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table("row_reviews", recreate="always") as batch:
            batch.drop_constraint("ck_row_reviews_verdict", type_="check")
            batch.alter_column(
                "row_id",
                existing_type=sa.String(200),
                type_=sa.Text(),
                existing_nullable=False,
            )
        return
    op.alter_column(
        "row_reviews",
        "row_id",
        existing_type=sa.String(200),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.execute("ALTER TABLE row_reviews DROP CONSTRAINT IF EXISTS ck_row_reviews_verdict;")
