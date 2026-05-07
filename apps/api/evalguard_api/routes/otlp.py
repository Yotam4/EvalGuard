"""``POST /v1/otlp/v1/traces`` — OTLP/HTTP JSON ingest.

Accepts a standard OTel ``ExportTraceServiceRequest`` JSON body,
extracts the GenAI semantic-convention attributes from each span,
and synthesizes EvalGuard runs that flow through the same
``_persist_run`` path the CLI ingest uses. From the API contract's
perspective, OTLP-derived runs are indistinguishable from
CLI-pushed runs except for ``runs.source = 'otlp'``.

Auth model and tenancy mirror ``/v1/runs``:
- Bearer-authed via the same middleware.
- The OTel ``service.name`` resource attribute (or
  ``evalguard.project`` if the user wants to be explicit) becomes
  the project slug — the run lands in the caller's org under that
  project, auto-creating it on first sight.
- Members can only ingest into their own org; admins can ingest
  into any (the project upsert is always scoped to
  ``principal.org_id``).

Response shape mirrors the OTLP/HTTP spec: ``{partialSuccess: {}}``
on 2xx so the OTel collector treats it as successful.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import upsert_project
from evalguard_api.deps import get_conn
from evalguard_api.otlp import OtlpParseError, parse_traces
from evalguard_api.routes.runs import _persist_run

router = APIRouter()


@router.post(
    "/v1/otlp/v1/traces",
    status_code=status.HTTP_200_OK,
    tags=["otlp"],
    # Loose response model — OTel collectors only care about the
    # 2xx status and an empty ``partialSuccess`` body. Documenting
    # a stricter shape would be misleading since we may add
    # rejection details later.
    response_model=None,
)
async def ingest_traces(
    request: Request,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    # Manual body read so we can return the OTel-shaped error before
    # Pydantic blows up on a non-OTLP payload. The request size cap
    # in main.py's middleware has already gated oversize bodies.
    try:
        body: Any = await request.json()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Body is not valid JSON: {e}",
        )

    settings = request.app.state.settings
    try:
        synthetic_runs = parse_traces(
            body,
            default_project=settings.default_project_slug,
        )
    except OtlpParseError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e),
        )

    # Persist each synthetic run in turn. Each lands in the caller's
    # org under the project named by ``service.name`` (or the
    # default). If two ResourceSpans blocks name the same project,
    # ``upsert_project`` returns the same project_id for both.
    accepted = 0
    for payload in synthetic_runs:
        project_id = upsert_project(
            conn, org_id=principal.org_id, project_name=payload["project"],
        )
        _persist_run(conn, payload, project_id, principal, source="otlp")
        accepted += 1

    # OTLP/HTTP spec: a successful ingest returns
    # ``ExportTraceServiceResponse`` with an empty ``partialSuccess``.
    # We add a small ``evalguard`` envelope (non-spec) so curl-driven
    # debugging shows what landed; the OTel collector ignores
    # unknown fields.
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "partialSuccess": {},
            "evalguard": {
                "accepted_runs": accepted,
                "ingested_by":   principal.key_id,
            },
        },
    )
