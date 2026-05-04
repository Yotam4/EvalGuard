"""``GET /v1/health`` — uptime + mode advertisement.

Used by load balancers, smoke tests, and deployment dashboards.
The ``mode`` field intentionally exposes whether auth is configured
so an operator running ``curl /v1/health`` from outside the cluster
gets a loud signal that the deployment is unauthenticated.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evalguard_api import __version__

router = APIRouter()


@router.get("/v1/health", tags=["health"])
def health(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "status":  "ok",
        "version": __version__,
        "mode":    "open" if settings.is_open_mode else "auth",
        "db":      _db_kind(settings.database_url),
    }


def _db_kind(url: str) -> str:
    if url.startswith("sqlite://"):
        return "sqlite"
    if url.startswith(("postgres://", "postgresql://")):
        return "postgres"
    return "unknown"
