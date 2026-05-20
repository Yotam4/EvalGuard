"""``/v1/golden/*`` — Phase OBS-4 golden-candidates staging.

Three surfaces:

- ``POST /v1/golden/candidates`` — promote a row.  UPSERT keyed on
  ``(run_id, row_id, promoted_by)`` so re-clicking the UI button is
  idempotent; submitting again updates the reviewer's note.
- ``GET /v1/projects/{slug}/golden/candidates`` — list staged
  candidates for one project, newest-first.
- ``DELETE /v1/golden/candidates/{id}`` — un-promote.  Only the
  original promoter (or admin) can delete; same constraint as
  ``row_reviews``.

The staging area is intentionally write-once-per-reviewer: a
future ``evalguard golden export`` CLI subcommand will read this
table and append to the operator's on-disk JSONL dataset, after
which the rows here serve as the audit trail of "what got
promoted, by whom, when".

Tenant scoping uses the same anti-enumeration 404 shape as
``/v1/reviews``: a cross-org or missing combo collapses to a
single 404 with a uniform detail.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import get_project_by_slug, now_iso
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    GoldenCandidate, GoldenCandidateIngest, GoldenCandidateList,
)


router = APIRouter()


# ---------------------------------------------------------------------------
# POST /v1/golden/candidates


@router.post(
    "/v1/golden/candidates",
    response_model=GoldenCandidate,
    status_code=status.HTTP_201_CREATED,
    tags=["golden"],
)
def promote(
    body: GoldenCandidateIngest,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> GoldenCandidate:
    """Promote one row to the golden staging area.

    UPSERT keyed on ``(run_id, row_id, promoted_by)`` — re-clicking
    the UI button updates the existing record's ``note`` in place
    and preserves ``created_at``.  Two different reviewers
    promoting the same row produce two separate records (so the
    note → reviewer attribution is preserved).
    """
    # Cross-org gate + project resolution in one query.  Mirrors
    # ``routes/reviews.py:submit_review``.
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

    # Confirm the row actually exists on that run — a typo'd
    # ``row_id`` shouldn't materialise a candidate against nothing.
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

    # UPSERT: SELECT-then-INSERT/UPDATE inside the same transaction.
    # Avoids per-dialect ``ON CONFLICT`` syntax differences; same
    # pattern as ``routes/reviews.py:submit_review``.
    existing = conn.execute(
        text("""SELECT id, created_at FROM golden_candidates
                WHERE run_id = :run_id AND row_id = :row_id
                  AND promoted_by = :promoter"""),
        {"run_id": body.run_id, "row_id": body.row_id,
         "promoter": principal.key_id},
    ).mappings().fetchone()

    if existing:
        conn.execute(
            text("""UPDATE golden_candidates
                    SET note = :note
                    WHERE id = :id"""),
            {"note": note, "id": existing["id"]},
        )
        candidate_id = existing["id"]
        created_at   = existing["created_at"]
    else:
        result = conn.execute(
            text("""INSERT INTO golden_candidates(
                      run_id, row_id, project_id, promoted_by,
                      note, created_at)
                    VALUES (:run_id, :row_id, :project_id, :promoter,
                            :note, :now)"""),
            {"run_id": body.run_id, "row_id": body.row_id,
             "project_id": project_id, "promoter": principal.key_id,
             "note": note, "now": now},
        )
        candidate_id = int(result.lastrowid) if result.lastrowid else 0
        created_at   = now

    return GoldenCandidate(
        id=candidate_id,
        run_id=body.run_id,
        row_id=body.row_id,
        project_id=project_id,
        promoted_by=principal.key_id,
        note=note,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/golden/candidates


@router.get(
    "/v1/projects/{project_slug}/golden/candidates",
    response_model=GoldenCandidateList,
    tags=["golden"],
)
def list_for_project(
    project_slug: str,
    limit: int = Query(default=100, ge=1, le=500),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> GoldenCandidateList:
    """List the project's staged candidates, newest-first.  Same
    project-visibility gate as ``GET /v1/projects/{slug}/calls``."""
    project = get_project_by_slug(
        conn, org_id=principal.org_id, slug=project_slug,
    )
    if project is None and principal.is_admin:
        row = conn.execute(
            text("SELECT * FROM projects WHERE slug = :slug LIMIT 1"),
            {"slug": project_slug},
        ).mappings().fetchone()
        project = dict(row) if row else None
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_slug!r} not found.",
        )

    rows = conn.execute(
        text("""SELECT id, run_id, row_id, project_id, promoted_by,
                       note, created_at
                FROM golden_candidates
                WHERE project_id = :project_id
                ORDER BY created_at DESC, id DESC
                LIMIT :limit"""),
        {"project_id": project["project_id"], "limit": limit},
    ).mappings().fetchall()
    return GoldenCandidateList(
        candidates=[GoldenCandidate(**dict(r)) for r in rows],
    )


# ---------------------------------------------------------------------------
# DELETE /v1/golden/candidates/{id}


@router.delete(
    "/v1/golden/candidates/{candidate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["golden"],
)
def un_promote(
    candidate_id: int,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> None:
    """Delete a candidate.  Only the original promoter (or an admin)
    can delete — a 403 (not 404) when a non-promoter tries, because
    the candidate's existence is already known via the parent
    project's listing; hiding it as 404 would be cosmetic only."""
    row = conn.execute(
        text("""SELECT golden_candidates.promoted_by,
                       golden_candidates.project_id,
                       projects.org_id AS owning_org_id
                FROM golden_candidates
                JOIN projects ON projects.project_id = golden_candidates.project_id
                WHERE golden_candidates.id = :id"""),
        {"id": candidate_id},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Candidate {candidate_id} not found.",
        )
    # Cross-org member sees the same 404 as a missing id — anti-
    # enumeration.  A member of the candidate's own org but who
    # isn't the promoter gets the more honest 403.
    if not principal.is_admin and row["owning_org_id"] != principal.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Candidate {candidate_id} not found.",
        )
    if not principal.is_admin and row["promoted_by"] != principal.key_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the original promoter or an admin can delete this candidate.",
        )
    conn.execute(
        text("DELETE FROM golden_candidates WHERE id = :id"),
        {"id": candidate_id},
    )
