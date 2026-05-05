"""``/v1/orgs/{org_id}/api_keys`` and ``/v1/api_keys/{key_id}``.

API-key lifecycle. Three operations:

- ``POST /v1/orgs/{org_id}/api_keys`` — mint a new key in that org.
  Requires the caller to be a member of ``org_id`` (or an admin).
  Plaintext token is in the response **once** and never again.
- ``GET /v1/orgs/{org_id}/api_keys`` — list keys (no plaintext, ever).
- ``DELETE /v1/api_keys/{key_id}`` — soft-revoke. Member of the
  key's org can revoke; admins can revoke any.

Privileged scopes (``admin``) are reserved for admins to grant —
non-admin members can only create org-scoped keys (empty scopes).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status

from evalguard_api.auth import (
    Principal, require_admin, require_org_member, require_principal,
)
from evalguard_api.db import (
    connect, create_api_key, get_api_key, get_org, list_api_keys_for_org,
    revoke_api_key,
)
from evalguard_api.models import (
    ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeySummary,
)

router = APIRouter()


def _conn(request: Request) -> sqlite3.Connection:
    settings = request.app.state.settings
    return connect(settings.sqlite_path or ":memory:")


def _to_summary(row: dict) -> ApiKeySummary:
    """Project a DB row into the listing shape (no plaintext)."""
    return ApiKeySummary(
        key_id=row["key_id"],
        org_id=row["org_id"],
        prefix=row["prefix"],
        name=row["name"],
        scopes=[s for s in row["scopes_csv"].split(",") if s],
        created_at=row["created_at"],
        revoked_at=row.get("revoked_at"),
        last_used_at=row.get("last_used_at"),
    )


# ---------------------------------------------------------------------------
# POST /v1/orgs/{org_id}/api_keys


@router.post(
    "/v1/orgs/{org_id}/api_keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["api_keys"],
)
def create(
    org_id: str,
    body: ApiKeyCreate,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> ApiKeyCreated:
    require_org_member(principal, org_id)

    # Privileged scopes need admin. Without this guard, an org member
    # could grant their key ``admin`` and escalate cross-org.
    if "admin" in body.scopes:
        require_admin(principal)

    conn = _conn(request)
    try:
        if get_org(conn, org_id) is None:
            # Should be unreachable for admins (require_org_member
            # passes them) — but covers the edge of an admin asking
            # about a non-existent org.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Org {org_id!r} not found.",
            )
        plaintext, row = create_api_key(
            conn, org_id=org_id, name=body.name, scopes=body.scopes,
        )
        return ApiKeyCreated(
            key=_to_summary(row),
            token=plaintext,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /v1/orgs/{org_id}/api_keys


@router.get(
    "/v1/orgs/{org_id}/api_keys",
    response_model=ApiKeyList,
    tags=["api_keys"],
)
def list_(
    org_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> ApiKeyList:
    require_org_member(principal, org_id)
    conn = _conn(request)
    try:
        return ApiKeyList(keys=[_to_summary(r) for r in list_api_keys_for_org(conn, org_id)])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DELETE /v1/api_keys/{key_id}


@router.delete(
    "/v1/api_keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api_keys"],
)
def revoke(
    key_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    conn = _conn(request)
    try:
        existing = get_api_key(conn, key_id)
        if existing is None:
            # No information leak — same response whether the key
            # exists in another org or doesn't exist at all.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Key {key_id!r} not found.",
            )
        # Members of the key's org may revoke their own keys; admins
        # may revoke any. ``require_org_member`` enforces both.
        require_org_member(principal, existing["org_id"])

        if existing.get("revoked_at"):
            # Idempotent: re-revoking a revoked key is a no-op success.
            return None
        revoke_api_key(conn, key_id)
        return None
    finally:
        conn.close()
