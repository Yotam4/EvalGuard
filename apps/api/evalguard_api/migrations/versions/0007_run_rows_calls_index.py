"""Add ``run_rows.ingested_at`` + ``output_preview`` + calls-stream index.

Phase OBS-1.  Three changes that together let the new project-wide
calls stream paginate over thousands of rows without a JOIN:

1. ``run_rows.ingested_at TEXT`` — denormalised from
   ``runs.ingested_at`` at insert time (``_persist_run`` change in
   the same commit).  Backfilled for older rows from the parent
   ``runs`` row.
2. ``run_rows.output_preview TEXT`` — first ~240 chars of
   ``trials[].rows[].output`` so the stream card can show a snippet
   without re-parsing ``payload_json``.  Backfill is intentionally
   skipped: parsing ``payload_json`` for every row would block the
   migration on a large DB.  Older rows render with an empty
   preview; new ingests populate it from day one.
3. Composite index ``idx_run_rows_calls(project_id, ingested_at, id)``
   — drives both the ``tab=recent`` (ORDER BY ingested_at DESC, id
   DESC) and ``tab=failures`` (WHERE passed=0 ORDER BY …) queries
   without a sort step.

Idempotent: inspects columns + index list before adding so a
fresh-DB install (where ``metadata.create_all`` already produced
the columns and index from the current MetaData) is a no-op.

Revision ID: 0007_run_rows_calls_index
Revises: 0006_row_reviews_verdict_check
Create Date: 2026-05-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_run_rows_calls_index"
down_revision: Union[str, Sequence[str], None] = "0006_row_reviews_verdict_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("run_rows")}

    # 1. ``ingested_at`` — TEXT, nullable so the backfill UPDATE can
    # run without a NOT NULL violation against the table-create
    # default.  After backfill, every existing row has a value and
    # new inserts via ``_persist_run`` always stamp it.
    if "ingested_at" not in cols:
        with op.batch_alter_table("run_rows") as batch:
            batch.add_column(sa.Column("ingested_at", sa.Text()))

    # 2. ``output_preview`` — same nullable shape; backfill is
    # intentionally deferred (see module docstring) so older rows
    # carry NULL until they're re-ingested.
    if "output_preview" not in cols:
        with op.batch_alter_table("run_rows") as batch:
            batch.add_column(sa.Column("output_preview", sa.Text()))

    # Backfill ``ingested_at`` from the parent ``runs`` row.  The
    # subquery is bounded by ``run_rows.run_id`` which already has
    # the ``idx_run_rows_run`` index, so the cost is a nested-loop
    # join with the index on the inner side — linear in the row
    # count.  On a brand-new DB ``run_rows`` is empty and this is a
    # no-op.
    #
    # ``COALESCE`` keeps the UPDATE idempotent: if a partial earlier
    # run already populated ``ingested_at`` for some rows, we don't
    # overwrite them.
    op.execute(
        "UPDATE run_rows SET ingested_at = "
        "  (SELECT runs.ingested_at FROM runs "
        "   WHERE runs.run_id = run_rows.run_id) "
        "WHERE ingested_at IS NULL"
    )

    # 3. Composite index for the calls-stream paginator.  Same
    # inspect-then-create pattern as 0003 so the fresh-DB path
    # (where ``metadata.create_all`` already produced the index) is
    # a no-op.
    existing_indexes = {ix["name"] for ix in insp.get_indexes("run_rows")}
    if "idx_run_rows_calls" not in existing_indexes:
        op.create_index(
            "idx_run_rows_calls",
            "run_rows",
            ["project_id", "ingested_at", "id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in insp.get_indexes("run_rows")}
    if "idx_run_rows_calls" in existing_indexes:
        op.drop_index("idx_run_rows_calls", table_name="run_rows")

    cols = {c["name"] for c in insp.get_columns("run_rows")}
    if "output_preview" in cols:
        with op.batch_alter_table("run_rows") as batch:
            batch.drop_column("output_preview")
    if "ingested_at" in cols:
        with op.batch_alter_table("run_rows") as batch:
            batch.drop_column("ingested_at")
