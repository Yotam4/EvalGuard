"""Bearer-token authentication.

MVP semantics:

- ``EVALGUARD_API_KEY`` env var configures a single shared secret.
  Every authenticated request must carry
  ``Authorization: Bearer <key>``. Constant-time compare so HTTP
  request timing doesn't leak the key.
- Missing env var → "open mode": every request authenticates as
  ``open-mode`` automatically, and the startup banner + health
  endpoint advertise that the server is unauthenticated. Suitable
  for local dev only — do NOT deploy without the env var.

A per-org API-keys table exists in the schema for the eventual
multi-tenant deploy (Phase 2.5); the MVP doesn't read it yet.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request, status

from evalguard_api.config import Settings


@dataclass(frozen=True)
class Principal:
    """Resolved caller identity. ``open-mode`` callers get a
    sentinel id so audit / ingest records distinguish them from
    real authenticated traffic."""

    key_id: str
    is_open_mode: bool


_OPEN_MODE_PRINCIPAL = Principal(key_id="open-mode", is_open_mode=True)


def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """FastAPI dependency: returns the resolved Principal or raises 401.

    Configured via ``app.state.settings`` — set by the lifespan.
    """
    settings: Settings = request.app.state.settings
    if settings.is_open_mode:
        return _OPEN_MODE_PRINCIPAL

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": 'Bearer realm="evalguard"'},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must be 'Bearer <token>'.",
            headers={"WWW-Authenticate": 'Bearer realm="evalguard"'},
        )
    if not secrets.compare_digest(token, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": 'Bearer realm="evalguard"'},
        )
    # Single-key MVP — every authenticated caller is the same principal.
    return Principal(key_id="env:EVALGUARD_API_KEY", is_open_mode=False)
