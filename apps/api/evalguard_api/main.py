"""FastAPI application entry.

The lifespan handler:

1. Builds a SQLAlchemy ``Engine`` from settings (SQLite default,
   Postgres if ``EVALGUARD_DATABASE_URL`` points there).
2. Runs ``alembic upgrade head`` programmatically — replaces the
   Phase-1 ``init_schema()`` with proper schema versioning so future
   schema changes ship as Alembic migrations.
3. Provisions the default org + project so the very first push has
   a tenant to attach to.
4. Idempotently materializes ``EVALGUARD_API_KEY`` as an admin
   api_keys row.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from evalguard_api.config import Settings, load_settings
from evalguard_api.db import (
    apply_admin_rls_context, create_api_key, ensure_default_tenancy,
    hash_token, key_hash_exists, make_engine,
)
from evalguard_api.routes.api_keys import router as api_keys_router
from evalguard_api.routes.assets import router as assets_router
from evalguard_api.routes.health import router as health_router
from evalguard_api.routes.orgs import router as orgs_router
from evalguard_api.routes.otlp import router as otlp_router
from evalguard_api.routes.projects import router as projects_router
from evalguard_api.routes.runs import router as runs_router

logger = logging.getLogger("evalguard.api")


def _alembic_config(database_url: str) -> AlembicConfig:
    """Build an Alembic ``Config`` programmatically.

    Programmatic upgrade doesn't require the ``alembic.ini`` file —
    the .ini exists for CLI invocations (``alembic revision``,
    ``alembic upgrade``) which operators run by hand. Building the
    config in code instead means the wheel doesn't need to ship a
    config file at a specific path, and the migration script
    location is unambiguous regardless of where the package is
    installed.
    """
    here = Path(__file__).resolve().parent
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(here / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    if settings.is_open_mode:
        logger.warning(
            "EvalGuard API starting in OPEN mode — no API key configured. "
            "Do NOT expose this server beyond local dev."
        )
    elif "*" in settings.cors_origins:
        # Auth is on but CORS is wide open — a browser at any origin
        # can land a tab at the API with the user's bearer token in
        # localStorage and have free reign. This is the default
        # because dev convenience matters more than the warning, but
        # production deployments should set EVALGUARD_CORS_ORIGINS
        # to an explicit allowlist.
        logger.warning(
            "EvalGuard API CORS is wide open ('*'). "
            "Set EVALGUARD_CORS_ORIGINS to a comma-separated allowlist "
            "(e.g. 'https://app.example.com') before exposing the "
            "server to a browser-reachable network."
        )

    # 1. Build the engine. The same engine is used by every route
    #    through ``request.app.state.engine`` for connection-pool reuse.
    engine = make_engine(settings)
    app.state.engine = engine

    # 2. Apply all pending Alembic migrations. Idempotent —
    #    ``alembic_version`` tracks the applied revision; re-startups
    #    are no-ops once head has been reached.
    alembic_command.upgrade(_alembic_config(settings.database_url), "head")

    # 3. Default org / project + admin-key bootstrap.
    #
    # The lifespan transaction needs to bypass RLS — it's writing to
    # ``orgs`` / ``projects`` / ``api_keys`` BEFORE any user request,
    # so there's no caller-org to scope to. Admin context lets the
    # writes through; per-request transactions in ``deps.get_conn``
    # supersede it with the actual principal.
    with engine.begin() as conn:
        apply_admin_rls_context(conn)
        org_id, _ = ensure_default_tenancy(
            conn,
            org_slug=settings.default_org_slug,
            project_slug=settings.default_project_slug,
        )
        if settings.api_key:
            # Existence check ignores revoked_at — if an operator
            # revoked the env-bootstrap key, restarting the server
            # must not silently re-create it (the UNIQUE constraint
            # on hashed_key would crash the lifespan, AND the
            # operator's revocation intent would be undone). To
            # rotate, either set EVALGUARD_API_KEY to a fresh value
            # or manually delete the old row.
            if not key_hash_exists(conn, hash_token(settings.api_key)):
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

    logger.info(
        "EvalGuard API ready · dialect=%s · mode=%s",
        engine.dialect.name,
        "open" if settings.is_open_mode else "auth",
    )
    try:
        yield
    finally:
        engine.dispose()


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
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["authorization", "content-type"],
        allow_credentials=False,
    )

    # Reject oversize bodies BEFORE Pydantic parses them — a 1 GB
    # POST would otherwise sit in memory while we marshal a 422.
    # We only check the advertised ``Content-Length``; chunked
    # uploads without a length header bypass this check, but every
    # plausible client (httpx, curl, requests, urllib) sends a
    # length on POST bodies.
    @app.middleware("http")
    async def _enforce_max_body(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                length = int(cl)
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header."},
                )
            if length > settings.max_request_bytes:
                return JSONResponse(
                    # Starlette renamed the constant in 0.40 ish;
                    # use the literal status code so we work across
                    # versions without conditional imports.
                    status_code=413,
                    content={
                        "detail":
                            f"Request body {length} bytes exceeds the "
                            f"{settings.max_request_bytes}-byte limit. Set "
                            f"EVALGUARD_MAX_REQUEST_BYTES higher to allow "
                            f"larger ingests, or split the run."
                    },
                )
        return await call_next(request)
    app.include_router(health_router)
    app.include_router(orgs_router)
    app.include_router(projects_router)
    app.include_router(api_keys_router)
    app.include_router(runs_router)
    app.include_router(assets_router)
    app.include_router(otlp_router)
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
