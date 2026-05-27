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

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import get_project_by_slug, now_iso
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    GoldenCandidate, GoldenCandidateIngest, GoldenCandidateList,
    GoldenRowData,
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

    # UPSERT.  Two-step (SELECT-then-INSERT/UPDATE) under a
    # SAVEPOINT so a concurrent same-reviewer click can't crash
    # the request with the UNIQUE constraint — if the INSERT
    # races, we catch ``IntegrityError``, roll back the savepoint,
    # and fall through to the SELECT+UPDATE branch.  Same pattern
    # as ``db.py:upsert_project``.
    #
    # ``lastrowid`` is unreliable across dialects: ``None`` on
    # Postgres, ``int`` on SQLite.  After every code path we
    # re-SELECT the canonical ``id`` and ``created_at`` so the
    # response body always matches what's in the table — no more
    # ``"id": 0`` placeholders in the response.

    def _update_existing(existing_row) -> tuple[int, str]:
        conn.execute(
            text("""UPDATE golden_candidates
                    SET note = :note
                    WHERE id = :id"""),
            {"note": note, "id": existing_row["id"]},
        )
        return existing_row["id"], existing_row["created_at"]

    def _select_existing():
        return conn.execute(
            text("""SELECT id, created_at FROM golden_candidates
                    WHERE run_id = :run_id AND row_id = :row_id
                      AND promoted_by = :promoter"""),
            {"run_id": body.run_id, "row_id": body.row_id,
             "promoter": principal.key_id},
        ).mappings().fetchone()

    existing = _select_existing()
    if existing:
        candidate_id, created_at = _update_existing(existing)
    else:
        try:
            with conn.begin_nested():
                conn.execute(
                    text("""INSERT INTO golden_candidates(
                              run_id, row_id, project_id, promoted_by,
                              note, created_at)
                            VALUES (:run_id, :row_id, :project_id, :promoter,
                                    :note, :now)"""),
                    {"run_id": body.run_id, "row_id": body.row_id,
                     "project_id": project_id, "promoter": principal.key_id,
                     "note": note, "now": now},
                )
        except IntegrityError:
            # Concurrent same-reviewer click won the race.  The other
            # transaction inserted the row; we update its note to
            # match the latest request (last write wins for the note
            # field, same semantics as the non-racing path).
            existing = _select_existing()
            if existing is None:
                # Shouldn't happen — the UNIQUE constraint must have
                # been satisfied by a row that exists.  Surface as a
                # 500 so the operator notices something is wrong with
                # the DB rather than us silently swallowing it.
                raise
            candidate_id, created_at = _update_existing(existing)
        else:
            # Successful INSERT — re-SELECT to get the canonical id
            # rather than depending on ``lastrowid`` (None on
            # Postgres).
            row = _select_existing()
            assert row is not None, "row vanished immediately after INSERT"
            candidate_id = row["id"]
            created_at   = row["created_at"]

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
    expand: str | None = Query(default=None,
        description="``row`` attaches each candidate's input / expected / "
                    "output content (one extra payload parse per distinct "
                    "run).  Omit for the lightweight metadata-only list."),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> GoldenCandidateList:
    """List the project's staged candidates, newest-first.  Same
    project-visibility gate as ``GET /v1/projects/{slug}/calls``.

    With ``?expand=row`` each candidate carries a ``row_data`` block
    (input / expected / output) so the UI can render the curated
    rows inline and compose a JSONL download without an N+1 fan-out
    of its own.  The expansion is bounded by the ``limit`` cap and
    de-duplicates payload parses across candidates that share a run.
    """
    if expand is not None and expand != "row":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown expand {expand!r}. Allowed: 'row'.",
        )

    project = get_project_by_slug(
        conn, org_id=principal.org_id, slug=project_slug,
    )
    if project is None and principal.is_admin:
        row = conn.execute(
            # Deterministic admin-fallback pick when a slug is shared
            # across orgs (slugs are unique per-org, not globally) —
            # see the same note in ``routes/calls.py``.
            text("SELECT * FROM projects WHERE slug = :slug "
                 "ORDER BY created_at, project_id LIMIT 1"),
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

    row_data_by_key: dict[tuple[str, str], GoldenRowData] = {}
    if expand == "row" and rows:
        row_data_by_key = _expand_row_content(conn, rows)

    return GoldenCandidateList(
        candidates=[
            GoldenCandidate(
                **dict(r),
                row_data=row_data_by_key.get((r["run_id"], r["row_id"])),
            )
            for r in rows
        ],
    )


def _expand_row_content(
    conn: Connection,
    candidates,
) -> dict[tuple[str, str], GoldenRowData]:
    """Load the input / expected / output for each ``(run_id, row_id)``
    candidate by parsing the parent run's ``payload_json``.  A run
    that was deleted out-of-band simply has no entry — the caller
    leaves ``row_data`` as ``None``.

    Memory discipline: ``payload_json`` is the FULL run blob (can be
    tens of MB for a 10k-row run).  We fetch + parse + discard ONE
    run's payload at a time so peak memory is bounded by the single
    largest run, not by ``sum(payloads)`` across every distinct run
    the candidate set references.  At the 500-candidate cap a naive
    ``WHERE run_id IN (...)`` + ``fetchall()`` could otherwise pull
    hundreds of multi-MB blobs into memory simultaneously.

    A future optimisation is to denormalise input/expected/output
    onto ``run_rows`` at ingest (the way ``output_preview`` already
    is) so this never touches ``payload_json`` — tracked separately;
    that needs a migration + ingest change.
    """
    # Which row_ids do we actually need per run?  Avoids building
    # GoldenRowData for every row in a 50k-row run.
    wanted: dict[str, set[str]] = {}
    for c in candidates:
        wanted.setdefault(c["run_id"], set()).add(c["row_id"])
    if not wanted:
        return {}

    out: dict[tuple[str, str], GoldenRowData] = {}
    # Iterate distinct runs deterministically; one PK-indexed lookup
    # per run, payload discarded before the next iteration.
    for run_id in sorted(wanted):
        need = wanted[run_id]
        row = conn.execute(
            text("SELECT payload_json FROM runs WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).mappings().fetchone()
        if row is None:
            continue   # orphaned candidate — leave row_data None.
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        remaining = set(need)
        for trial in payload.get("trials") or []:
            for r in trial.get("rows") or []:
                rid = r.get("row_id")
                if rid in remaining:
                    out[(run_id, rid)] = GoldenRowData(
                        input=r.get("input"),
                        expected=r.get("expected"),
                        output=r.get("output"),
                    )
                    remaining.discard(rid)
            if not remaining:
                break   # found every wanted row in this run.
        # ``payload`` goes out of scope at the next loop iteration.
    return out


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
