"""``/v1/reviews`` — Argilla-style human review queue (Phase 4).

Three surfaces:

- ``GET /v1/reviews/queue?run_id=...`` — rows that automated checks
  flagged as failing and that the *caller* hasn't reviewed yet.
- ``POST /v1/reviews`` — submit a verdict. UPSERT keyed on
  ``(run_id, row_id, reviewer_key_id)``: re-submitting a verdict for
  a row you've already reviewed updates the existing review, never
  creates a duplicate or overwrites someone else's.
- ``GET /v1/runs/{run_id}/reviews`` — list all reviews on a run
  (every reviewer, every verdict).

Cross-tenant scoping mirrors ``/v1/runs``: a non-admin member can
only see / write reviews against rows in their own org. The check
collapses to "the row's project belongs to the caller's org" via a
JOIN through ``projects``; cross-org reviews return 404 with no
enumeration leak.

The queue policy starts simple: a row is "in the queue" iff at
least one gate_results entry for it failed AND the caller hasn't
already reviewed it. Future policies (judge confidence bands, manual
``needs_review`` tags) extend the WHERE clause without changing the
endpoint shape.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import now_iso
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    ReviewIngest, ReviewListResponse, ReviewOut,
    ReviewQueueItem, ReviewQueueResponse,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /v1/reviews/queue


@router.get(
    "/v1/reviews/queue",
    response_model=ReviewQueueResponse,
    tags=["reviews"],
)
def queue(
    run_id:    str  = Query(..., description="The run whose failing rows to queue."),
    limit:     int  = Query(default=50, ge=1, le=200),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> ReviewQueueResponse:
    """Rows in ``run_id`` that:
    1. have at least one failing ``gate_results`` entry, AND
    2. the calling reviewer (``principal.key_id``) hasn't reviewed yet.

    A cross-org or missing ``run_id`` collapses to 404 (same shape as
    ``GET /v1/runs/{id}``). Admins see queues for any run.
    """
    # Run-visibility gate first so we don't reveal "this run exists
    # in a different org" via the empty-list 200.
    row = conn.execute(
        text("""SELECT runs.run_id, projects.org_id AS owning_org_id
                FROM runs
                JOIN projects ON projects.project_id = runs.project_id
                WHERE runs.run_id = :run_id"""),
        {"run_id": run_id},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )
    if not principal.is_admin and row["owning_org_id"] != principal.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )

    # Queue policy: rows where the automated verdict says fail
    # (``rr.passed = 0``) and this reviewer hasn't already weighed in.
    # Gates are trial-scoped, not row-scoped, so we use them as
    # *context* for the reviewer (``failing_gates``) rather than the
    # inclusion filter — a trial-level gate failure doesn't tell us
    # which particular row to look at, but the row's own
    # ``passed=0`` does.
    #
    # ``failing_gate_names`` is a comma-joined fallback that works on
    # both SQLite (``GROUP_CONCAT(x, ',')``) and Postgres
    # (``STRING_AGG``); we use the SQLite spelling since
    # ``GROUP_CONCAT`` is also supported by Postgres-via-extension and
    # the cost is negligible at the queue's bounded size.
    rows = conn.execute(
        text("""
            SELECT
              rr.row_id            AS row_id,
              rr.trial_id          AS trial_id,
              rr.project_id        AS project_id,
              rr.passed            AS passed,
              rr.cost_usd          AS cost_usd,
              rr.latency_ms        AS latency_ms,
              rr.tags_json         AS tags_json,
              GROUP_CONCAT(g.gate_name, ',')  AS failing_gate_names
            FROM run_rows rr
            LEFT JOIN gate_results g
              ON g.run_id = rr.run_id
             AND COALESCE(g.trial_id, '') = COALESCE(rr.trial_id, '')
             AND g.passed = 0
            LEFT JOIN row_reviews rev
              ON rev.run_id  = rr.run_id
             AND rev.row_id  = rr.row_id
             AND rev.reviewer_key_id = :reviewer
            WHERE rr.run_id = :run_id
              AND rr.passed = 0
              AND rev.id IS NULL
            GROUP BY rr.id, rr.row_id, rr.trial_id, rr.project_id, rr.passed,
                     rr.cost_usd, rr.latency_ms, rr.tags_json
            ORDER BY rr.id ASC
            LIMIT :limit
        """),
        {"run_id": run_id, "reviewer": principal.key_id, "limit": limit},
    ).mappings().fetchall()

    import json as _json
    items: list[ReviewQueueItem] = []
    for r in rows:
        try:
            tags = _json.loads(r["tags_json"]) if r["tags_json"] else []
        except (TypeError, ValueError):
            tags = []
        failing_gates = sorted(set(
            (r["failing_gate_names"] or "").split(",")
        )) if r["failing_gate_names"] else []
        # Strip any blanks from the GROUP_CONCAT artefact.
        failing_gates = [g for g in failing_gates if g]
        items.append(ReviewQueueItem(
            run_id=run_id,
            row_id=r["row_id"],
            trial_id=r["trial_id"],
            project_id=r["project_id"],
            passed=bool(r["passed"]),
            cost_usd=float(r["cost_usd"] or 0.0),
            latency_ms=int(r["latency_ms"] or 0),
            tags=tags if isinstance(tags, list) else [],
            failing_gates=failing_gates,
        ))
    return ReviewQueueResponse(items=items, run_id=run_id)


# ---------------------------------------------------------------------------
# POST /v1/reviews


@router.post(
    "/v1/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
    tags=["reviews"],
)
def submit_review(
    body: ReviewIngest,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ReviewOut:
    """Submit a review verdict. The composite key is
    ``(run_id, row_id, reviewer_key_id)`` — re-submitting for a row
    the same reviewer has already touched UPDATEs the existing
    review (note + verdict refresh, ``updated_at`` advances) without
    creating a duplicate. Two different reviewers reviewing the same
    row produce two separate rows.

    ``note`` empty-strings normalise to NULL on write so the GET
    side doesn't have to distinguish "" from "no note".
    """
    # Resolve the row's owning project + cross-tenant gate. The same
    # JOIN that ``get_run`` uses; an unauthorised caller gets 404.
    row = conn.execute(
        text("""SELECT runs.project_id AS project_id,
                       projects.org_id AS owning_org_id
                FROM runs
                JOIN projects ON projects.project_id = runs.project_id
                WHERE runs.run_id = :run_id"""),
        {"run_id": body.run_id},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {body.run_id!r} not found.",
        )
    if not principal.is_admin and row["owning_org_id"] != principal.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {body.run_id!r} not found.",
        )

    # Confirm the row actually exists on that run. Saves a confusing
    # success on a typo'd ``row_id``.
    exists = conn.execute(
        text("""SELECT 1 FROM run_rows
                WHERE run_id = :run_id AND row_id = :row_id LIMIT 1"""),
        {"run_id": body.run_id, "row_id": body.row_id},
    ).first()
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Row {body.row_id!r} not found on run {body.run_id!r}.",
        )

    note = body.note.strip() if body.note else None
    if note == "":
        note = None

    project_id = row["project_id"]
    now = now_iso()

    # UPSERT keyed on the unique composite. We don't use
    # ``ON CONFLICT`` SQL syntax because the SQLite dialect needs a
    # different incantation than Postgres — a SELECT-then-INSERT/
    # UPDATE inside the same transaction works on both and is small
    # enough that the round-trip cost is fine for a human-paced
    # endpoint.
    existing = conn.execute(
        text("""SELECT id, created_at FROM row_reviews
                WHERE run_id = :run_id AND row_id = :row_id
                  AND reviewer_key_id = :reviewer"""),
        {"run_id":   body.run_id, "row_id": body.row_id,
         "reviewer": principal.key_id},
    ).mappings().fetchone()

    if existing:
        conn.execute(
            text("""UPDATE row_reviews
                    SET verdict = :verdict, note = :note, updated_at = :now
                    WHERE id = :id"""),
            {"verdict": body.verdict, "note": note, "now": now,
             "id": existing["id"]},
        )
        review_id  = existing["id"]
        created_at = existing["created_at"]
    else:
        result = conn.execute(
            text("""INSERT INTO row_reviews(
                      run_id, row_id, project_id, reviewer_key_id,
                      verdict, note, created_at, updated_at)
                    VALUES (:run_id, :row_id, :project_id, :reviewer,
                            :verdict, :note, :now, :now)"""),
            {"run_id": body.run_id, "row_id": body.row_id,
             "project_id": project_id, "reviewer": principal.key_id,
             "verdict": body.verdict, "note": note, "now": now},
        )
        review_id  = int(result.lastrowid) if result.lastrowid else 0
        created_at = now

    return ReviewOut(
        id=review_id,
        run_id=body.run_id,
        row_id=body.row_id,
        project_id=project_id,
        reviewer_key_id=principal.key_id,
        verdict=body.verdict,
        note=note,
        created_at=created_at,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/reviews


@router.get(
    "/v1/runs/{run_id}/reviews",
    response_model=ReviewListResponse,
    tags=["reviews"],
)
def list_reviews_for_run(
    run_id: str,
    conn:   Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ReviewListResponse:
    """All reviews on ``run_id`` across every reviewer. Tenant-scoped
    via the same 404-on-foreign rule as ``GET /v1/runs/{id}``."""
    row = conn.execute(
        text("""SELECT runs.run_id, projects.org_id AS owning_org_id
                FROM runs
                JOIN projects ON projects.project_id = runs.project_id
                WHERE runs.run_id = :run_id"""),
        {"run_id": run_id},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )
    if not principal.is_admin and row["owning_org_id"] != principal.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )

    rows = conn.execute(
        text("""SELECT id, run_id, row_id, project_id, reviewer_key_id,
                       verdict, note, created_at, updated_at
                FROM row_reviews WHERE run_id = :run_id
                ORDER BY created_at ASC, id ASC"""),
        {"run_id": run_id},
    ).mappings().fetchall()
    return ReviewListResponse(reviews=[
        ReviewOut(**dict(r)) for r in rows
    ])
