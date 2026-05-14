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

Phase 3c: spans are filtered through ``sampling.filter_otlp_spans``
before parsing.  ``EVALGUARD_OTLP_SAMPLE_RATE=0.1`` keeps ~10 %% of
traces deterministically (same traceId ⇒ same answer across
restarts).  Drops are silent at the OTLP layer; the response body's
``evalguard`` envelope reports the kept / dropped span counts so
the caller can see what landed without grepping logs.

Response shape mirrors the OTLP/HTTP spec: ``{partialSuccess: {}}``
on 2xx so the OTel collector treats it as successful.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Connection

from pydantic import ValidationError
from sqlalchemy import text

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import upsert_project
from evalguard_api.deps import get_conn
from evalguard_api.models import RunIngest
from evalguard_api.otlp import OtlpParseError, parse_traces
from evalguard_api.routes.runs import _persist_run
from evalguard_api.sampling import filter_otlp_spans

router = APIRouter()
logger = logging.getLogger("evalguard.api.otlp")


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

    # Phase 3c: head-based sampler.  Filtering before parse so the
    # parser only sees the spans we're going to persist — keeps the
    # parser pure and the sampler observable in one place.  rate=1.0
    # is a fast no-op (just counts spans); rates < 1.0 walk the body.
    body, kept_spans, dropped_spans = filter_otlp_spans(
        body, settings.otlp_sample_rate,
    )

    try:
        synthetic_runs = parse_traces(
            body,
            default_project=settings.default_project_slug,
            actor_id=principal.key_id or "otlp",
            actor_type="api_key",
        )
    except OtlpParseError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e),
        )

    # Persist each synthetic run. The synthesized payload must
    # validate through ``RunIngest`` for the same reason the regular
    # ``POST /v1/runs`` body does — schema correctness, cardinality
    # caps, regex-checked ids. Without this, an OTLP path could
    # write rows the JSON Schema validator would later reject on GET.
    #
    # Each run lands in the caller's org under the project named by
    # ``service.name`` (or the default). If two ResourceSpans blocks
    # name the same project, ``upsert_project`` returns the same
    # project_id for both.
    accepted = 0
    duplicates = 0
    for payload in synthetic_runs:
        try:
            validated = RunIngest.model_validate(payload)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"OTLP-synthesized payload failed schema validation: {e.errors()[:3]}",
            )
        # ``run_id`` is deterministic from the trace id; a collector
        # retry hashes to the same value. If the run already exists,
        # the OTLP contract is to ack 200 with no work (collectors
        # treat 200 as final-success) — 409 would trigger an
        # exponential-backoff retry that never converges.
        already = conn.execute(
            text("SELECT 1 FROM runs WHERE run_id = :rid"),
            {"rid": validated.run_id},
        ).fetchone()
        if already is not None:
            duplicates += 1
            continue
        project_id = upsert_project(
            conn, org_id=principal.org_id, project_name=validated.project,
        )
        # Use ``model_dump`` so ``_persist_run`` sees the validated
        # (and field-aliased) shape, not the raw synthesized dict.
        _persist_run(
            conn,
            validated.model_dump(mode="json"),
            project_id,
            principal,
            source="otlp",
        )
        accepted += 1

    # Structured log line: the access-log middleware records the
    # request envelope; this adds the OTLP-specific counters so an
    # operator chasing "where did half my traces go?" can confirm
    # the sampler's contribution.
    if dropped_spans or settings.otlp_sample_rate < 1.0:
        # JSON-encode the line so it's parseable downstream (a log
        # aggregator's JSON filter will choke on Python's ``%r``
        # single-quoted strings).
        import json as _json
        logger.info(_json.dumps({
            "evt":           "otlp.sample",
            "kept_spans":    kept_spans,
            "dropped_spans": dropped_spans,
            "rate":          settings.otlp_sample_rate,
            "accepted_runs": accepted,
            "key_id":        principal.key_id,
            "org_id":        principal.org_id,
        }))

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
                "accepted_runs":   accepted,
                # Collector retries hash to the same run_id; we count
                # how many landed as duplicates so an operator can see
                # whether something upstream is double-sending.
                "duplicate_runs":  duplicates,
                # Phase 3c — head-based sampler. ``kept_spans`` /
                # ``dropped_spans`` are populated by the sampler before
                # ``parse_traces`` runs; an operator can see drop rates
                # at the response level.
                "kept_spans":     kept_spans,
                "dropped_spans":  dropped_spans,
                "sample_rate":    settings.otlp_sample_rate,
                "ingested_by":    principal.key_id,
            },
        },
    )
