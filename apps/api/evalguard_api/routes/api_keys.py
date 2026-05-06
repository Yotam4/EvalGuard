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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.engine import Connection

from evalguard_api.auth import (
    Principal, require_admin, require_org_member, require_principal,
)
from evalguard_api.db import (
    create_api_key, get_api_key, get_org, list_api_keys_for_org,
    revoke_api_key,
)
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeySummary,
)

router = APIRouter()


def _to_summary(row: dict) -> ApiKeySummary:
    """Project a DB row into the listing shape (no plaintext)."""
    return ApiKeySummary(
        key_id=row["key_id"],
        org_id=row["org_id"],
        prefix=row["prefix"],
        name=row["name"],
        scopes=[s.strip() for s in row["scopes_csv"].split(",") if s.strip()],
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
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ApiKeyCreated:
    require_org_member(principal, org_id)

    # Privilege escalation guard: only admins can mint admin keys.
    if "admin" in body.scopes:
        require_admin(principal)

    if get_org(conn, org_id) is None:
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


# ---------------------------------------------------------------------------
# GET /v1/orgs/{org_id}/api_keys


@router.get(
    "/v1/orgs/{org_id}/api_keys",
    response_model=ApiKeyList,
    tags=["api_keys"],
)
def list_(
    org_id: str,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ApiKeyList:
    require_org_member(principal, org_id)
    return ApiKeyList(keys=[_to_summary(r) for r in list_api_keys_for_org(conn, org_id)])


# ---------------------------------------------------------------------------
# DELETE /v1/api_keys/{key_id}


@router.delete(
    "/v1/api_keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["api_keys"],
)
def revoke(
    key_id: str,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
):
    existing = get_api_key(conn, key_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key {key_id!r} not found.",
        )
    require_org_member(principal, existing["org_id"])

    if existing.get("revoked_at"):
        # Idempotent: re-revoking a revoked key is a no-op success.
        return None
    revoke_api_key(conn, key_id)
    return None
