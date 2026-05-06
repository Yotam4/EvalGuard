"""``/v1/projects`` — project CRUD scoped to the caller's org.

Phase-1 ingest auto-creates projects on first push; this slice adds
explicit CRUD for operators who want to create projects ahead of any
runs (e.g. wiring CI before the first eval). Org scoping is implicit:
the project lives in ``principal.org_id``, never specified by the
client.

Admin callers can target a different org via ``?org_id=...``; the
parameter is intentionally a query string (not a header) so it shows
up in API logs and audit dashboards.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.engine import Connection

from evalguard_api.auth import (
    Principal, require_org_member, require_principal,
)
from evalguard_api.db import (
    create_project_explicit, get_org, get_project_by_slug,
    list_projects_for_org,
)
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    ProjectCreate, ProjectList, ProjectOut,
)

router = APIRouter()


def _resolve_target_org(
    principal: Principal, requested_org: str | None,
) -> str:
    """Decide which org the operation targets.

    - No ``org_id`` query param → caller's own org (the common case).
    - Explicit ``org_id`` AND admin → that org.
    - Explicit ``org_id`` AND non-admin → 403 (member can't escape).
    """
    if requested_org is None:
        return principal.org_id
    if not principal.is_admin and requested_org != principal.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot target a different org without admin scope.",
        )
    return requested_org


# ---------------------------------------------------------------------------
# POST /v1/projects


@router.post("/v1/projects", response_model=ProjectOut,
             status_code=status.HTTP_201_CREATED, tags=["projects"])
def create(
    body: ProjectCreate,
    org_id: str | None = Query(default=None,
                               description="Admin-only: target a different org."),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectOut:
    target_org = _resolve_target_org(principal, org_id)
    require_org_member(principal, target_org)
    if get_org(conn, target_org) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Org {target_org!r} not found.",
        )
    if get_project_by_slug(conn, org_id=target_org, slug=body.slug) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project {body.slug!r} already exists in org {target_org!r}.",
        )
    row = create_project_explicit(
        conn, org_id=target_org, slug=body.slug, name=body.name,
    )
    return ProjectOut(**row)


# ---------------------------------------------------------------------------
# GET /v1/projects


@router.get("/v1/projects", response_model=ProjectList, tags=["projects"])
def list_(
    org_id: str | None = Query(default=None),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectList:
    target_org = _resolve_target_org(principal, org_id)
    rows = list_projects_for_org(conn, target_org)
    return ProjectList(projects=[ProjectOut(**r) for r in rows])


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}


@router.get("/v1/projects/{slug}", response_model=ProjectOut, tags=["projects"])
def get(
    slug: str,
    org_id: str | None = Query(default=None),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectOut:
    target_org = _resolve_target_org(principal, org_id)
    row = get_project_by_slug(conn, org_id=target_org, slug=slug)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {slug!r} not found in org {target_org!r}.",
        )
    return ProjectOut(**row)
