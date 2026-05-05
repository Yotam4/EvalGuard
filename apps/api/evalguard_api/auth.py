"""Bearer-token authentication backed by the ``api_keys`` table.

The Phase-2 server resolves every request to a ``Principal`` carrying
the caller's ``org_id``, the ``key_id`` that authorized them, and a
list of ``scopes``. Every downstream route can then enforce
multi-tenancy by checking the principal's org against the resource's
org, and check scope membership for cross-org operations.

Token shape: ``evk_<32 hex>`` (see ``db.generate_token``). The
plaintext is returned to the caller exactly once, on creation; the
DB stores ``sha256(token)`` only.

Bootstrap: ``EVALGUARD_API_KEY`` env var is materialized as an
``admin``-scoped key in the default org on startup, so existing
single-tenant deployments and the Phase-1 CLI flow keep working with
zero config change.

Open mode: ``EVALGUARD_API_KEY`` empty → every request authenticates
as a synthetic admin in the default org. Loud startup banner +
``mode: open`` advertised in ``GET /v1/health``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from fastapi import Header, HTTPException, Request, status

from evalguard_api.config import Settings
from evalguard_api.db import connect, find_key_by_hash, hash_token


# ---------------------------------------------------------------------------
# Principal


@dataclass(frozen=True)
class Principal:
    """The resolved caller. Every authenticated route handler depends
    on this; downstream code uses ``org_id`` to scope writes/reads
    and ``has_scope('admin')`` to gate cross-org operations."""

    key_id:       str
    org_id:       str
    scopes:       tuple[str, ...]
    is_open_mode: bool

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def is_admin(self) -> bool:
        """Admin = scoped 'admin' OR open-mode dev sentinel.

        Both kinds bypass org checks. Open-mode is for local dev only
        (advertised on /v1/health) so the relaxed check is intentional.
        """
        return self.is_open_mode or "admin" in self.scopes


# Sentinel principal returned when the server runs without an API key.
# Carries ``admin`` semantics so every cross-org operation just works
# in dev. Bound to the configured default org so writes have somewhere
# to land.
def _open_mode_principal(default_org_id: str) -> Principal:
    return Principal(
        key_id="open-mode",
        org_id=default_org_id,
        scopes=("admin",),
        is_open_mode=True,
    )


# ---------------------------------------------------------------------------
# Dependency: resolve the calling Principal


def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """FastAPI dependency: returns the resolved Principal or raises 401.

    The lookup strategy is:

    1. ``settings.is_open_mode`` (no env key) → return open-mode admin.
    2. Authorization header missing / wrong scheme → 401.
    3. Token hash misses the api_keys table (or matches a revoked
       row) → 401.
    4. Otherwise return Principal(key.org_id, key.scopes).
    """
    settings: Settings = request.app.state.settings

    if settings.is_open_mode:
        # Default org is provisioned in the lifespan, so the org_id is
        # guaranteed to exist.  We construct the slug-based id without
        # a DB roundtrip; ``ensure_default_tenancy`` mints the same id.
        return _open_mode_principal(default_org_id=f"org_{settings.default_org_slug}")

    if not authorization:
        _challenge("Missing Authorization header.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        _challenge("Authorization must be 'Bearer <token>'.")

    # Hash and look up. ``find_key_by_hash`` filters out revoked keys.
    conn = connect(settings.sqlite_path or ":memory:")
    try:
        key_row = find_key_by_hash(conn, hash_token(token))
    finally:
        conn.close()
    if key_row is None:
        _challenge("Invalid or revoked API key.")

    return Principal(
        key_id=key_row["key_id"],
        org_id=key_row["org_id"],
        scopes=tuple(s for s in key_row["scopes_csv"].split(",") if s),
        is_open_mode=False,
    )


def _challenge(detail: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer realm="evalguard"'},
    )


# ---------------------------------------------------------------------------
# Helper guards used by the org / project / api-key routes.


def require_admin(principal: Principal) -> None:
    """Raise 403 unless the principal has the ``admin`` scope.

    Use as ``require_admin(principal)`` *inside* a handler so the
    dependency stays a single ``Principal`` (FastAPI doesn't compose
    typed dependencies as cleanly as Starlette guards)."""
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin scope required.",
        )


def require_org_member(principal: Principal, org_id: str) -> None:
    """Raise 403 unless the principal can act on ``org_id``.

    Members of the org pass; admins pass cross-org. Anyone else gets
    403.  This is the single chokepoint for every cross-org guard so
    a future RLS layer can mirror it 1:1.
    """
    if principal.org_id == org_id or principal.is_admin:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Caller is not a member of this organization.",
    )


def filter_orgs_visible_to(
    principal: Principal, all_orgs: Iterable[dict],
) -> list[dict]:
    """Project a full ``orgs`` listing down to what the principal is
    allowed to see. Admins see all; org members see only their own.

    Helpful at the listing layer where issuing a 403 per-row would
    leak existence — instead the response is silently scoped.
    """
    if principal.is_admin:
        return list(all_orgs)
    return [o for o in all_orgs if o["org_id"] == principal.org_id]
