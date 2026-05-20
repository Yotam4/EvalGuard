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
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

from evalguard_api.config import Settings, load_settings, validate_for_startup
from evalguard_api.db import (
    apply_admin_rls_context, create_api_key, ensure_default_tenancy,
    hash_token, key_hash_exists, make_engine,
)
from evalguard_api.routes.api_keys import router as api_keys_router
from evalguard_api.routes.assets import router as assets_router
from evalguard_api.routes.calls import router as calls_router
from evalguard_api.routes.health import router as health_router
from evalguard_api.routes.orgs import router as orgs_router
from evalguard_api.routes.otlp import router as otlp_router
from evalguard_api.routes.projects import router as projects_router
from evalguard_api.routes.reviews import router as reviews_router
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

    # Refuse-to-boot: catches the dangerous combinations explicitly
    # (open mode without opt-in, open mode with CORS=*) before any
    # network listener binds.
    validate_for_startup(settings)

    if settings.is_open_mode:
        logger.warning(
            "EvalGuard API starting in OPEN mode (EVALGUARD_OPEN_MODE=1) — "
            "no API key configured. Do NOT expose this server beyond "
            "local dev."
        )
    if "*" in settings.cors_origins and not settings.is_open_mode:
        logger.warning(
            "EvalGuard API CORS is wide open ('*') with auth enabled. "
            "Browser tabs at any origin can call the API with a stolen "
            "bearer token. Set EVALGUARD_CORS_ORIGINS to a comma-separated "
            "allowlist for any browser-reachable deployment."
        )

    # 1. Build the engine. The same engine is used by every route
    #    through ``request.app.state.engine`` for connection-pool reuse.
    engine = make_engine(settings)
    app.state.engine = engine

    # 2. BYPASSRLS guard — refuse to boot if the runtime DB role can
    #    bypass row-level security. RLS is the *only* defense-in-depth
    #    layer beyond the application-layer auth; a runtime role that
    #    silently sees every row defeats the entire 0002/0003 layer.
    #    Migrations should run as a separate ``evalguard_migrator``
    #    role that has BYPASSRLS; the runtime role must not.
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT bypassrls FROM pg_roles WHERE rolname = current_user"
            )).first()
            if row and row[0]:
                raise RuntimeError(
                    "EvalGuard refuses to start: the runtime DB role "
                    f"({conn.execute(text('SELECT current_user')).scalar()}) "
                    "has BYPASSRLS. RLS in migrations 0002/0003 is the "
                    "only DB-layer tenancy enforcement; a BYPASSRLS role "
                    "silently disables it. Run the API as a non-superuser "
                    "role; let migrations run as a separate role."
                )

    # 3. Apply all pending Alembic migrations. Idempotent —
    #    ``alembic_version`` tracks the applied revision; re-startups
    #    are no-ops once head has been reached.
    alembic_command.upgrade(_alembic_config(settings.database_url), "head")

    # 4. Default org / project + admin-key bootstrap.
    #
    # On Postgres, wrap the whole bootstrap in a session-level advisory
    # lock so two concurrently-launched workers (gunicorn pre-fork,
    # k8s rolling restart) don't both pass ``key_hash_exists`` and
    # then crash on the UNIQUE constraint when the second one inserts.
    # The lock auto-releases when the connection is closed, so even if
    # the lifespan crashes mid-bootstrap, the next startup acquires
    # cleanly.
    #
    # The lifespan transaction also needs to bypass RLS — it's writing
    # to ``orgs`` / ``projects`` / ``api_keys`` BEFORE any user
    # request, so there's no caller-org to scope to. Admin context
    # lets the writes through; per-request transactions in
    # ``deps.get_conn`` supersede it with the actual principal.
    _BOOTSTRAP_LOCK = 0x45_56_61_6C_47_72_64_31  # ASCII 'EvalGrd1'

    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _BOOTSTRAP_LOCK},
            )
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
    # Ensure the root logger has at least one handler so the structured
    # ``_access_log`` middleware (and any module-level ``logger.info``
    # call) actually reaches stderr. ``main.run()`` configures logging
    # explicitly, but ``python -m uvicorn evalguard_api.main:app`` skips
    # that path — without this the SOC 2 audit trail is silently
    # disabled. ``basicConfig`` is a no-op when handlers already exist
    # (pytest configures them, ``main.run()`` configures them, gunicorn
    # configures them) so this is safe to call unconditionally.
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stderr,
        )
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
    # ``HTTPSRedirectMiddleware`` must run before any other middleware
    # that touches the URL — install first. ``TrustedHostMiddleware``
    # next so spoofed Host headers are rejected before CORS pre-flight
    # logic looks at the Origin. Both are env-gated so local dev on
    # http://localhost still works without flags.
    if settings.require_https:
        app.add_middleware(HTTPSRedirectMiddleware)
    if settings.trusted_hosts and settings.trusted_hosts != ("*",):
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(settings.trusted_hosts),
        )
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
    # Structured access log: one JSON line per response. Required
    # for SOC 2 / ISO 27001 — the audit trail needs to know which
    # api_key called which route with what outcome and how long it
    # took. ``key_id`` is populated by ``require_principal`` setting
    # ``request.state.principal`` (when auth ran); for unauthenticated
    # paths (``/v1/health``) the field is omitted.
    @app.middleware("http")
    async def _access_log(request: Request, call_next):
        import json as _json
        import time as _time
        t0 = _time.monotonic()
        # Read these BEFORE call_next — once the response is returned
        # the request scope may have been consumed.
        method = request.method
        # Use the matched route's path template (``/v1/runs/{run_id}``)
        # rather than the literal URL so logs aggregate cleanly. Fall
        # back to the raw path if the router hasn't matched yet.
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            principal = getattr(request.state, "principal", None)
            entry = {
                "evt":         "http.request",
                "method":      method,
                "path":        request.url.path,
                "status":      500,
                "duration_ms": elapsed_ms,
                "key_id":      getattr(principal, "key_id", None),
                "org_id":      getattr(principal, "org_id", None),
                "exception":   True,
            }
            logger.info(_json.dumps({k: v for k, v in entry.items() if v is not None}))
            raise
        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        principal = getattr(request.state, "principal", None)
        route = request.scope.get("route")
        path_template = getattr(route, "path", None) or request.url.path
        entry = {
            "evt":         "http.request",
            "method":      method,
            "path":        path_template,
            "status":      response.status_code,
            "duration_ms": elapsed_ms,
            "key_id":      getattr(principal, "key_id", None),
            "org_id":      getattr(principal, "org_id", None),
        }
        logger.info(_json.dumps({k: v for k, v in entry.items() if v is not None}))
        return response

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
    app.include_router(reviews_router)
    app.include_router(calls_router)
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
