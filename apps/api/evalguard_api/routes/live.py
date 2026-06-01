"""``/v1/projects/{slug}/live/*`` — Phase PROXY-2.5 timeline + aggregate views.

The "verbose live-run inspection" surfaces:

- ``GET /v1/projects/{slug}/live/timeline?days=N`` — the last N daily
  live runs with their aggregates.  Drives the calendar-bar UI strip
  on ``/calls/`` so the operator can see "5,000 calls 97.5% pass" at
  a glance per day.
- ``GET /v1/projects/{slug}/live/aggregate?from=&to=`` — SUM of the
  same aggregates over an arbitrary window.  Powers the timeline
  header banner and drag-select range readouts.

Both endpoints are read-only and project-scoped via the same anti-
enumeration 404 shape as the rest of the API.  Aggregates come
directly from ``runs.row_*`` counter columns — those are stamped
atomically by ``live.record_call`` inside the row-insert transaction
(PROXY-2), so a SUM matches the live ground truth without scanning
``run_rows``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import get_project_by_slug
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    LiveAggregate, LiveTimelineEntry, LiveTimelineResponse,
)


router = APIRouter()


# Bounds on the timeline horizon.  90 daily entries renders cleanly
# in a ~600px horizontal strip without ellipsis; further back is
# better served by the explicit ``?from=&to=`` aggregate endpoint
# (which a "last year" report would hit, not the timeline UI).
_TIMELINE_MAX_DAYS = 90


def _resolve_project(
    conn: Connection, principal: Principal, slug: str,
) -> dict:
    """Same project-resolution + anti-enumeration shape used in
    ``routes/configs.py`` / ``routes/golden.py`` / ``routes/calls.py``."""
    project = get_project_by_slug(
        conn, org_id=principal.org_id, slug=slug,
    )
    if project is None and principal.is_admin:
        row = conn.execute(
            text("SELECT * FROM projects WHERE slug = :slug "
                 "ORDER BY created_at, project_id LIMIT 1"),
            {"slug": slug},
        ).mappings().fetchone()
        project = dict(row) if row else None
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {slug!r} not found.",
        )
    return project


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/live/timeline


@router.get(
    "/v1/projects/{project_slug}/live/timeline",
    response_model=LiveTimelineResponse,
    tags=["live"],
)
def get_live_timeline(
    project_slug: str,
    days: int = Query(default=30, ge=1, le=_TIMELINE_MAX_DAYS,
        description="Number of most-recent daily live runs to return."),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> LiveTimelineResponse:
    """Recent daily live runs for one project, newest-first.

    Used by the timeline strip on ``/calls/`` — each entry renders as
    one horizontal bar with pass-rate + cost + call-count, click to
    filter the stream to that day's window.
    """
    project = _resolve_project(conn, principal, project_slug)

    rows = conn.execute(
        text("""SELECT run_id, started_at, finished_at,
                       row_count, row_pass_count, row_fail_count, cost_usd
                FROM runs
                WHERE project_id = :pid AND source = 'live'
                ORDER BY started_at DESC NULLS LAST, run_id DESC
                LIMIT :limit"""
             if conn.dialect.name == "postgresql"
             else """SELECT run_id, started_at, finished_at,
                       row_count, row_pass_count, row_fail_count, cost_usd
                FROM runs
                WHERE project_id = :pid AND source = 'live'
                ORDER BY started_at DESC, run_id DESC
                LIMIT :limit"""),
        {"pid": project["project_id"], "limit": days},
    ).mappings().fetchall()

    return LiveTimelineResponse(
        entries=[
            LiveTimelineEntry(
                run_id=r["run_id"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                row_count=int(r["row_count"] or 0),
                row_pass_count=int(r["row_pass_count"] or 0),
                row_fail_count=int(r["row_fail_count"] or 0),
                cost_usd=float(r["cost_usd"] or 0.0),
            )
            for r in rows
        ],
    )


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/live/aggregate


@router.get(
    "/v1/projects/{project_slug}/live/aggregate",
    response_model=LiveAggregate,
    tags=["live"],
)
def get_live_aggregate(
    project_slug: str,
    from_: str | None = Query(default=None, alias="from",
        description="Inclusive lower bound on ``runs.started_at`` (ISO-8601)."),
    to:    str | None = Query(default=None,
        description="Exclusive upper bound on ``runs.started_at`` (ISO-8601)."),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> LiveAggregate:
    """SUM of pass / fail / cost across the live runs that fall in
    ``[from, to)``.  Omit both bounds for "all-time" (the project's
    full live history).

    Half-open interval so a drag-select across adjacent days doesn't
    double-count the boundary minute — matches the calls list's
    ``?from=&to=`` contract.
    """
    project = _resolve_project(conn, principal, project_slug)

    clauses = ["project_id = :pid", "source = 'live'"]
    params: dict = {"pid": project["project_id"]}
    if from_ is not None:
        clauses.append("started_at >= :from_ts")
        params["from_ts"] = from_
    if to is not None:
        clauses.append("started_at < :to_ts")
        params["to_ts"] = to
    where = " AND ".join(clauses)

    # ``COALESCE`` so an empty window returns zero counters rather
    # than NULL — the UI's header banner divides by row_count to get
    # the pass-rate and the zero-call case shows "—" cleanly.
    row = conn.execute(
        text(f"""SELECT COALESCE(SUM(row_count), 0)      AS row_count,
                        COALESCE(SUM(row_pass_count), 0) AS row_pass_count,
                        COALESCE(SUM(row_fail_count), 0) AS row_fail_count,
                        COALESCE(SUM(cost_usd), 0)       AS cost_usd,
                        COUNT(*)                          AS run_count
                 FROM runs WHERE {where}"""),
        params,
    ).mappings().fetchone()

    return LiveAggregate(
        row_count=int(row["row_count"] or 0),
        row_pass_count=int(row["row_pass_count"] or 0),
        row_fail_count=int(row["row_fail_count"] or 0),
        cost_usd=float(row["cost_usd"] or 0.0),
        run_count=int(row["run_count"] or 0),
    )
