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
from evalguard_api.db import resolve_project_or_404
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


# Project-resolution + cross-org 404 — see ``db.py:resolve_project_or_404``.
_resolve_project = resolve_project_or_404


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
        description="Inclusive lower bound on ``run_rows.ingested_at`` (ISO-8601)."),
    to:    str | None = Query(default=None,
        description="Exclusive upper bound on ``run_rows.ingested_at`` (ISO-8601)."),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> LiveAggregate:
    """SUM of pass / fail / cost across the proxied calls that fall in
    ``[from, to)``.  Omit both bounds for "all-time" (the project's
    full live history).

    Half-open interval so a drag-select across adjacent days doesn't
    double-count the boundary minute — matches the calls list's
    ``?from=&to=`` contract on **the same column** (per-row
    ``ingested_at``).  An earlier implementation filtered the daily
    ``runs.started_at`` buckets, which produced disagreeing
    banner-vs-list counts for non-day-aligned windows: a window like
    ``28T12 → 29T12`` excluded the May-28 daily run (its
    ``started_at = 28T00 < from``) even though calls between
    ``28T12`` and ``29T00`` were inside the window.  Summing
    ``run_rows`` directly keeps the banner exactly consistent with
    what the list shows; the composite ``idx_run_rows_calls``
    already covers the range scan.
    """
    project = _resolve_project(conn, principal, project_slug)

    # Sum directly from ``run_rows`` (the same source-of-truth the
    # /calls list reads from).  Live rows are identified by their
    # parent run's ``source = 'live'``; the IN subquery short-
    # circuits because ``runs.run_id`` is the PK.
    clauses = ["rr.project_id = :pid",
               "EXISTS (SELECT 1 FROM runs r "
               "        WHERE r.run_id = rr.run_id AND r.source = 'live')"]
    params: dict = {"pid": project["project_id"]}
    if from_ is not None:
        clauses.append("rr.ingested_at >= :from_ts")
        params["from_ts"] = from_
    if to is not None:
        clauses.append("rr.ingested_at < :to_ts")
        params["to_ts"] = to
    where = " AND ".join(clauses)

    # ``COALESCE`` so an empty window returns zero counters rather
    # than NULL — the UI's header banner divides by row_count to get
    # the pass-rate and the zero-call case shows "—" cleanly.  The
    # ``passed`` column is 0|1 so SUM(passed) is row_pass_count.
    row = conn.execute(
        text(f"""SELECT COUNT(*)                            AS row_count,
                        COALESCE(SUM(rr.passed), 0)         AS row_pass_count,
                        COALESCE(SUM(1 - rr.passed), 0)     AS row_fail_count,
                        COALESCE(SUM(rr.cost_usd), 0)       AS cost_usd,
                        COUNT(DISTINCT rr.run_id)           AS run_count
                 FROM run_rows AS rr
                 WHERE {where}"""),
        params,
    ).mappings().fetchone()

    return LiveAggregate(
        row_count=int(row["row_count"] or 0),
        row_pass_count=int(row["row_pass_count"] or 0),
        row_fail_count=int(row["row_fail_count"] or 0),
        cost_usd=float(row["cost_usd"] or 0.0),
        run_count=int(row["run_count"] or 0),
    )
