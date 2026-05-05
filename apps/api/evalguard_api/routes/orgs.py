"""``/v1/orgs`` — organization CRUD.

Multi-tenant authorization model:

- **Create** (``POST``) requires ``admin`` scope. Org creation is a
  privileged operation: a non-admin token can never spawn a new
  isolated tenant on a server that's already accepting their writes.
- **List** (``GET``) is silently scoped — admins see every org,
  org members see exactly their own. Returning 403 per-row would
  leak the existence of other orgs.
- **Detail** (``GET /{org_id}``) goes through ``require_org_member``
  so a member of org A asking for org B sees a clean 403, not the
  data and not a 404 (mirrors how GitHub answers /orgs/{name}).

There is intentionally no PUT or DELETE in this slice — org
mutation requires careful cascade handling that's out of MVP scope.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status

from evalguard_api.auth import (
    Principal, filter_orgs_visible_to, require_admin,
    require_org_member, require_principal,
)
from evalguard_api.db import (
    connect, create_org, get_org, get_org_by_slug, list_orgs,
)
from evalguard_api.models import OrgCreate, OrgList, OrgOut

router = APIRouter()


def _conn(request: Request) -> sqlite3.Connection:
    settings = request.app.state.settings
    return connect(settings.sqlite_path or ":memory:")


# ---------------------------------------------------------------------------
# POST /v1/orgs


@router.post("/v1/orgs", response_model=OrgOut,
             status_code=status.HTTP_201_CREATED, tags=["orgs"])
def create(
    body: OrgCreate,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> OrgOut:
    require_admin(principal)
    conn = _conn(request)
    try:
        if get_org_by_slug(conn, body.slug) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Org slug {body.slug!r} already exists.",
            )
        row = create_org(conn, slug=body.slug, name=body.name)
        return OrgOut(**row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /v1/orgs


@router.get("/v1/orgs", response_model=OrgList, tags=["orgs"])
def list_(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> OrgList:
    conn = _conn(request)
    try:
        rows = list_orgs(conn)
        # Scope down to what the caller may see. Org members see one
        # entry; admins see everything.
        visible = filter_orgs_visible_to(principal, rows)
        return OrgList(orgs=[OrgOut(**r) for r in visible])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /v1/orgs/{org_id}


@router.get("/v1/orgs/{org_id}", response_model=OrgOut, tags=["orgs"])
def get(
    org_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> OrgOut:
    require_org_member(principal, org_id)
    conn = _conn(request)
    try:
        row = get_org(conn, org_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Org {org_id!r} not found.",
            )
        return OrgOut(**row)
    finally:
        conn.close()
