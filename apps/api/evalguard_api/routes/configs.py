"""``/v1/projects/{slug}/config*`` — Phase PROXY-1 server-side project configs.

Three surfaces:

- ``POST /v1/projects/{slug}/config`` — upload an ``evalguard.yaml``
  blob.  Content-addressed by SHA-256: re-pushing identical bytes
  returns the existing record (200), a new revision lands as 201.
- ``GET  /v1/projects/{slug}/config`` — fetch the *latest* config for
  the project.  404 when none has ever been pushed.
- ``GET  /v1/projects/{slug}/config/history`` — list recent revisions,
  newest-first, content omitted (use the per-id GET to fetch a
  specific revision's bytes).
- ``GET  /v1/projects/{slug}/config/{config_id}`` — fetch one
  specific revision verbatim.  Used by the upcoming proxy invoke
  path when a caller pins to a specific config hash.

The server computes the SHA-256 itself rather than trusting a
client-supplied hash — the row's ``content_sha256`` is the integrity
guarantee, not a piece of caller-supplied metadata.

Tenant scoping uses the same anti-enumeration 404 shape as
``/v1/golden/*``: a cross-org or missing slug collapses to a single
404 with a uniform detail.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import get_project_by_slug, now_iso
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    ProjectConfig, ProjectConfigHistory, ProjectConfigIngest,
    ProjectConfigSummary,
)


router = APIRouter()


# Hard cap on how many history rows the list endpoint returns.  A
# project that's been pushed daily for two years still fits.
_HISTORY_MAX = 500


def _resolve_project(
    conn: Connection, principal: Principal, slug: str,
) -> dict:
    """Same project-resolution pattern as ``routes/golden.py`` /
    ``routes/calls.py``: org-scoped lookup, with an admin slug-fallback
    that picks deterministically when the same slug exists across
    orgs (slugs are unique per-org, not globally).  Cross-org and
    missing both surface as 404 with the same detail — anti-
    enumeration."""
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
# POST /v1/projects/{slug}/config


@router.post(
    "/v1/projects/{project_slug}/config",
    response_model=ProjectConfig,
    tags=["configs"],
)
def push_config(
    project_slug: str,
    body: ProjectConfigIngest,
    response: Response,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfig:
    """Upload an ``evalguard.yaml`` blob.

    Content-addressed: the server computes SHA-256 over the bytes and
    UPSERTs on ``(project_id, content_sha256)``.  Idempotent re-push
    of identical bytes returns the existing record with HTTP 200; a
    new revision lands as HTTP 201.

    Returns the canonical record (id + computed hash + stored pushed_at)
    so the client can pin to a specific revision later without
    re-uploading."""
    project = _resolve_project(conn, principal, project_slug)
    project_id = project["project_id"]

    sha = hashlib.sha256(body.content.encode("utf-8")).hexdigest()

    # Idempotency: if a row already exists for (project_id, sha256),
    # return it as 200 — same bytes, same revision, no new row.
    existing = conn.execute(
        text("""SELECT id, pushed_by, pushed_at
                FROM project_configs
                WHERE project_id = :project_id
                  AND content_sha256 = :sha
                LIMIT 1"""),
        {"project_id": project_id, "sha": sha},
    ).mappings().fetchone()
    if existing:
        response.status_code = status.HTTP_200_OK
        return ProjectConfig(
            id=existing["id"],
            project_id=project_id,
            content_sha256=sha,
            content=body.content,
            pushed_by=existing["pushed_by"],
            pushed_at=existing["pushed_at"],
        )

    now = now_iso()
    try:
        with conn.begin_nested():
            conn.execute(
                text("""INSERT INTO project_configs(
                          project_id, content_sha256, content,
                          pushed_by, pushed_at)
                        VALUES (:project_id, :sha, :content,
                                :pusher, :now)"""),
                {"project_id": project_id, "sha": sha,
                 "content": body.content, "pusher": principal.key_id,
                 "now": now},
            )
    except IntegrityError:
        # Concurrent push of the same bytes won the race.  Re-SELECT
        # and return as 200 (matches the idempotency contract above).
        existing = conn.execute(
            text("""SELECT id, pushed_by, pushed_at
                    FROM project_configs
                    WHERE project_id = :project_id
                      AND content_sha256 = :sha
                    LIMIT 1"""),
            {"project_id": project_id, "sha": sha},
        ).mappings().fetchone()
        if existing is None:
            raise   # unique-constraint fired without a row — surface as 500
        response.status_code = status.HTTP_200_OK
        return ProjectConfig(
            id=existing["id"],
            project_id=project_id,
            content_sha256=sha,
            content=body.content,
            pushed_by=existing["pushed_by"],
            pushed_at=existing["pushed_at"],
        )

    # Successful INSERT — re-SELECT for the canonical id (lastrowid
    # is None on Postgres).
    row = conn.execute(
        text("""SELECT id FROM project_configs
                WHERE project_id = :project_id
                  AND content_sha256 = :sha
                LIMIT 1"""),
        {"project_id": project_id, "sha": sha},
    ).mappings().fetchone()
    assert row is not None, "row vanished immediately after INSERT"

    response.status_code = status.HTTP_201_CREATED
    return ProjectConfig(
        id=row["id"],
        project_id=project_id,
        content_sha256=sha,
        content=body.content,
        pushed_by=principal.key_id,
        pushed_at=now,
    )


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/config


@router.get(
    "/v1/projects/{project_slug}/config",
    response_model=ProjectConfig,
    tags=["configs"],
)
def get_latest_config(
    project_slug: str,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfig:
    """Fetch the latest config revision for the project.  404 if the
    project exists but no config has been pushed yet."""
    project = _resolve_project(conn, principal, project_slug)

    row = conn.execute(
        text("""SELECT id, project_id, content_sha256, content,
                       pushed_by, pushed_at
                FROM project_configs
                WHERE project_id = :project_id
                ORDER BY pushed_at DESC, id DESC
                LIMIT 1"""),
        {"project_id": project["project_id"]},
    ).mappings().fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config has been pushed for project {project_slug!r}.",
        )
    return ProjectConfig(**dict(row))


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/config/history


@router.get(
    "/v1/projects/{project_slug}/config/history",
    response_model=ProjectConfigHistory,
    tags=["configs"],
)
def list_config_history(
    project_slug: str,
    limit: int = Query(default=20, ge=1, le=_HISTORY_MAX),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfigHistory:
    """List recent config revisions for the project, newest-first.
    ``content`` is omitted from each entry — fetch a specific
    revision's bytes via the per-id GET below."""
    project = _resolve_project(conn, principal, project_slug)

    rows = conn.execute(
        text("""SELECT id, project_id, content_sha256, pushed_by, pushed_at
                FROM project_configs
                WHERE project_id = :project_id
                ORDER BY pushed_at DESC, id DESC
                LIMIT :limit"""),
        {"project_id": project["project_id"], "limit": limit},
    ).mappings().fetchall()
    return ProjectConfigHistory(
        configs=[ProjectConfigSummary(**dict(r)) for r in rows],
    )


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/config/{config_id}


@router.get(
    "/v1/projects/{project_slug}/config/{config_id}",
    response_model=ProjectConfig,
    tags=["configs"],
)
def get_config_revision(
    project_slug: str,
    config_id: int,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfig:
    """Fetch one specific revision verbatim.  The id+slug pair must
    match — passing another project's id under this slug surfaces as
    a 404 to match the anti-enumeration shape."""
    project = _resolve_project(conn, principal, project_slug)

    row = conn.execute(
        text("""SELECT id, project_id, content_sha256, content,
                       pushed_by, pushed_at
                FROM project_configs
                WHERE id = :id AND project_id = :project_id"""),
        {"id": config_id, "project_id": project["project_id"]},
    ).mappings().fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config revision {config_id} not found for project {project_slug!r}.",
        )
    return ProjectConfig(**dict(row))
