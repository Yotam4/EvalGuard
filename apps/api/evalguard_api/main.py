"""FastAPI application entry.

The lifespan handler initializes the database (idempotent — every
CREATE TABLE is IF NOT EXISTS) and provisions the default org +
project so the very first push has a tenant to attach to.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from evalguard_api.config import Settings, load_settings
from evalguard_api.db import (
    connect, create_api_key, ensure_default_tenancy, find_key_by_hash,
    hash_token, init_schema,
)
from evalguard_api.routes.api_keys import router as api_keys_router
from evalguard_api.routes.health import router as health_router
from evalguard_api.routes.orgs import router as orgs_router
from evalguard_api.routes.projects import router as projects_router
from evalguard_api.routes.runs import router as runs_router

logger = logging.getLogger("evalguard.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    if settings.is_open_mode:
        logger.warning(
            "EvalGuard API starting in OPEN mode — no API key configured. "
            "Do NOT expose this server beyond local dev."
        )

    path = settings.sqlite_path
    if path is None:
        logger.warning(
            "Non-sqlite DATABASE_URL %r — Postgres support lands in Phase 2.5; "
            "current build only honours sqlite URLs.",
            settings.database_url,
        )
    else:
        conn = connect(path)
        try:
            init_schema(conn)
            org_id, _ = ensure_default_tenancy(
                conn,
                org_slug=settings.default_org_slug,
                project_slug=settings.default_project_slug,
            )
            # Idempotent admin-key bootstrap: when ``EVALGUARD_API_KEY``
            # is set, ensure an api_keys row exists with that token's
            # hash and ``admin`` scope. Re-startups are no-ops; rotating
            # the env var creates a new admin key (the old one stays
            # active until manually revoked, matching how operators
            # handle leaked credentials).
            if settings.api_key:
                existing = find_key_by_hash(conn, hash_token(settings.api_key))
                if existing is None:
                    create_api_key(
                        conn,
                        org_id=org_id,
                        name="bootstrap (env)",
                        scopes=["admin"],
                        token=settings.api_key,
                    )
                    logger.info(
                        "Bootstrap admin key materialized in org=%s "
                        "(from $EVALGUARD_API_KEY).", org_id,
                    )
        finally:
            conn.close()
        logger.info("EvalGuard API ready · db=%s · mode=%s",
                    path, "open" if settings.is_open_mode else "auth")
    yield


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Tests pass an explicit ``settings``
    (with a tmp-path sqlite DB and a known API key) so they don't
    depend on process env."""
    settings = settings or load_settings()
    app = FastAPI(
        title="EvalGuard API",
        version=__import__("evalguard_api").__version__,
        description=(
            "Phase-2 ingestion API. Accepts runs from `evalguard push` and "
            "serves them under `/v1/runs/{run_id}` matching the same JSON "
            "contract `evalguard view --json` produces."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type"],
        allow_credentials=False,
    )
    app.include_router(health_router)
    app.include_router(orgs_router)
    app.include_router(projects_router)
    app.include_router(api_keys_router)
    app.include_router(runs_router)
    return app


# Module-level app for production (uvicorn evalguard_api.main:app).
app = build_app()


def run() -> None:  # pragma: no cover — convenience CLI entry
    """``evalguard-api`` console-script — uvicorn-runs ``app``."""
    import uvicorn

    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    uvicorn.run(
        "evalguard_api.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_level="info",
    )
