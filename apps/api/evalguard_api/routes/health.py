"""Health + readiness probes.

Two endpoints with deliberately different contracts:

- ``GET /v1/health`` — **liveness**.  Always cheap, never touches the
  DB.  Returns 200 as long as the process is up.  Used as a k8s
  ``livenessProbe`` or "is the server breathing?" smoke check.

- ``GET /v1/ready`` — **readiness**.  Deeper: confirms the DB is
  reachable, that Alembic is at head, and that the evaluators
  registry can be loaded.  Returns 503 with a structured ``checks``
  payload when any of those fail so an LB / k8s ``readinessProbe``
  pulls the pod out of rotation cleanly.

The split matters: a shallow ``/v1/health`` that pings the DB will
mark a pod NotReady on a transient DB blip and cascade through
rolling restarts; a deep-only ``/v1/health`` will keep an
mis-migrated pod in rotation receiving real traffic.  Two probes
solves both problems.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from evalguard_api import __version__

router = APIRouter()
logger = logging.getLogger("evalguard.api.health")


@router.get("/v1/health", tags=["health"])
def health(request: Request) -> dict:
    """Liveness — process is up.  No DB ping, no migration check.
    The ``mode`` field intentionally exposes whether auth is
    configured so an operator running ``curl /v1/health`` from
    outside the cluster gets a loud signal that the deployment is
    unauthenticated."""
    settings = request.app.state.settings
    return {
        "status":  "ok",
        "version": __version__,
        "mode":    "open" if settings.is_open_mode else "auth",
        "db":      _db_kind(settings.database_url),
    }


@router.get("/v1/ready", tags=["health"])
def ready(request: Request) -> JSONResponse:
    """Readiness — the server can actually serve traffic.

    Three checks, each contributing to the overall verdict:

    - ``db``: round-trip a ``SELECT 1`` against the configured
      engine.  Catches DB unreachable, auth failures, connection-
      pool exhaustion (request to checkout times out).
    - ``migration``: read ``alembic_version`` and confirm it
      matches the script-directory head.  Catches "deployed new
      code against a stale schema" and "ran a partial migration"
      both — a pod in the middle of a rolling deploy that's at
      n-1 schema reports NotReady so the LB doesn't send traffic.
    - ``evaluators``: walk the entry-points registry once.  Catches
      a broken evaluator plugin install (rare but real — silently
      missing entry-points would make every ``/invoke`` call 422).

    On any failure: 503 with the same body shape so monitoring can
    parse ``ok`` regardless of HTTP status.
    """
    settings = request.app.state.settings
    engine   = request.app.state.engine

    checks: dict[str, dict] = {}
    overall_ok = True

    # --- DB --------------------------------------------------------
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["db"] = {"ok": True, "kind": _db_kind(settings.database_url)}
    except Exception as e:
        overall_ok = False
        checks["db"] = {
            "ok":    False,
            "kind":  _db_kind(settings.database_url),
            "error": f"{type(e).__name__}: {e}"[:240],
        }

    # --- Alembic at head ------------------------------------------
    # If the DB ping failed there's no point looking up the version
    # (would surface the same error noisily); just report unknown.
    if checks["db"]["ok"]:
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            import pathlib

            alembic_ini = pathlib.Path(__file__).resolve().parents[1] / "migrations"
            script_dir  = ScriptDirectory(str(alembic_ini))
            head_rev    = script_dir.get_current_head()

            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT version_num FROM alembic_version"),
                ).first()
            current_rev = row[0] if row else None

            if current_rev == head_rev:
                checks["migration"] = {"ok": True, "current": current_rev}
            else:
                overall_ok = False
                checks["migration"] = {
                    "ok":      False,
                    "current": current_rev,
                    "head":    head_rev,
                    "error":   "alembic version drift — run `alembic upgrade head`",
                }
        except Exception as e:
            overall_ok = False
            checks["migration"] = {
                "ok":    False,
                "error": f"{type(e).__name__}: {e}"[:240],
            }
    else:
        checks["migration"] = {"ok": False, "error": "db unreachable"}

    # --- Evaluators registry --------------------------------------
    try:
        from evalguard_evaluators.registry import iter_evaluators, iter_providers
        evs   = iter_evaluators()
        provs = iter_providers()
        if not evs or not provs:
            overall_ok = False
            checks["evaluators"] = {
                "ok":         False,
                "evaluators": len(evs),
                "providers":  len(provs),
                "error":      "no entry-points found — evaluator plugin install broken",
            }
        else:
            checks["evaluators"] = {
                "ok":         True,
                "evaluators": len(evs),
                "providers":  len(provs),
            }
    except Exception as e:
        overall_ok = False
        checks["evaluators"] = {
            "ok":    False,
            "error": f"{type(e).__name__}: {e}"[:240],
        }

    body = {
        "ok":      overall_ok,
        "version": __version__,
        "mode":    "open" if settings.is_open_mode else "auth",
        "checks":  checks,
    }
    if not overall_ok:
        # Log once at WARN so a transient NotReady leaves a trail an
        # operator can grep without scraping the LB.
        logger.warning("readiness probe failed: %s", checks)
    return JSONResponse(
        status_code=status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body,
    )


def _db_kind(url: str) -> str:
    if url.startswith("sqlite://"):
        return "sqlite"
    if url.startswith(("postgres://", "postgresql://")):
        return "postgres"
    return "unknown"
