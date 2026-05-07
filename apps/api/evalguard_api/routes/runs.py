"""``/v1/runs`` — ingest, list, fetch.

Three endpoints implementing the Phase-2 ``evalguard push`` target:

- ``POST /v1/runs`` — accept a run JSON, persist it, return the
  canonical URL. 409 on duplicate ``run_id`` (no implicit overwrite).
- ``GET  /v1/runs`` — list recent runs, scoped to caller's org
  (admins see all), filterable by project.
- ``GET  /v1/runs/{run_id}`` — fetch the full run JSON; 404 on
  cross-org access (no enumeration leak).

Every query is parameterised SQL via SQLAlchemy ``text()`` so it
works on SQLite (default) and Postgres (via the ``[postgres]``
extra) without backend-specific branching at the route layer.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import now_iso, upsert_project
from evalguard_api.deps import get_conn
from evalguard_api.models import RunIngest, RunList, RunOut, RunSummary

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /v1/runs


@router.post("/v1/runs", status_code=status.HTTP_201_CREATED, tags=["runs"])
def ingest_run(
    body: RunIngest,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    # The run lands in the *caller's* org — no client-supplied org
    # parameter, by design. The payload's ``project: <name>`` becomes
    # the project slug within that org.
    project_id = upsert_project(
        conn, org_id=principal.org_id, project_name=body.project,
    )

    # Conflict on duplicate run_id. Runs are immutable artefacts;
    # explicit replacement would need its own endpoint.
    existing = conn.execute(
        text("SELECT 1 FROM runs WHERE run_id=:run_id"),
        {"run_id": body.run_id},
    ).fetchone()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {body.run_id!r} already exists.",
        )

    # Persist with ``exclude_unset=True`` so optional fields the
    # client didn't send aren't materialized as ``null`` in the
    # stored payload. Without this, a minimal push (no audit, no
    # aggregate, no comparison) would round-trip with explicit
    # ``audit: null`` etc., which the JSON Schema's
    # ``additionalProperties: false`` definitions reject — i.e. a
    # GET would return JSON the schema's own validator wouldn't
    # accept.
    payload = body.model_dump(mode="json", exclude_unset=True)
    _persist_run(conn, payload, project_id, principal)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "run_id":     body.run_id,
            "url":        f"/v1/runs/{body.run_id}",
            "project_id": project_id,
        },
        headers={"Location": f"/v1/runs/{body.run_id}"},
    )


# ---------------------------------------------------------------------------
# GET /v1/runs


@router.get("/v1/runs", response_model=RunList, tags=["runs"])
def list_runs(
    project: str | None = Query(default=None, description="Filter by project slug."),
    limit: int = Query(default=20, ge=1, le=200),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> RunList:
    """List recent runs visible to the caller.

    Org members see only runs in their own org; admins see every
    run unless ``project`` narrows the listing further. The org
    filter is implicit so a member can never enumerate a foreign
    org's runs by listing without a filter.
    """
    # SQLAlchemy ``text()`` with named bind params — same syntax on
    # SQLite + Postgres. The org filter via correlated subquery means
    # we don't need to JOIN projects every row.
    clauses: list[str] = []
    params: dict = {"limit": limit}
    if not principal.is_admin:
        clauses.append("project_id IN (SELECT project_id FROM projects WHERE org_id = :org_id)")
        params["org_id"] = principal.org_id
    if project is not None:
        clauses.append("project_name = :project")
        params["project"] = project
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = text(
        f"""SELECT run_id, project_name AS project, status, gate_status,
                   started_at, finished_at,
                   row_count, row_pass_count, row_fail_count, cost_usd,
                   ingested_at, ingested_by, source
            FROM runs
            {where}
            ORDER BY ingested_at DESC
            LIMIT :limit"""
    )
    rows = conn.execute(sql, params).mappings().fetchall()
    return RunList(runs=[RunSummary(**dict(r)) for r in rows], next=None)


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}


@router.get("/v1/runs/{run_id}", response_model=RunOut, tags=["runs"])
def get_run(
    run_id: str,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> RunOut:
    """Fetch a run by id.

    Cross-org reads return **404** rather than 403 — exposing the
    existence of a run in another org would let a curious caller
    enumerate run_ids across tenants. The 404 is identical to the
    "this id never existed" response.
    """
    row = conn.execute(
        text("""SELECT runs.payload_json,
                       runs.ingested_at,
                       runs.ingested_by,
                       runs.project_id,
                       projects.org_id AS owning_org_id
                FROM runs
                JOIN projects ON projects.project_id = runs.project_id
                WHERE runs.run_id = :run_id"""),
        {"run_id": run_id},
    ).mappings().fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )
    if not principal.is_admin and row["owning_org_id"] != principal.org_id:
        # Same status / detail as the genuine 404 — no info leak.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )
    payload = json.loads(row["payload_json"])
    payload["server"] = {
        "ingested_at": row["ingested_at"],
        "ingested_by": row["ingested_by"],
        "project_id":  row["project_id"],
    }
    return RunOut.model_validate(payload)


# ---------------------------------------------------------------------------
# Internal — persistence helpers


def _persist_run(
    conn: Connection,
    payload: dict,
    project_id: str,
    principal: Principal,
    source: str = "cli",
) -> None:
    """Write the run header + denormalized trial / row / gate / asset /
    event rows. Caller's transaction wraps the whole thing; on any
    raised exception, ``deps.get_conn``'s ``engine.begin()`` rolls
    back atomically so partial state never lands.

    Mirrors the CLI's ``serializer.run_to_dict`` shape 1:1 — any drift
    in input shape is caught by Pydantic upstream of this call, so
    here we trust the payload structure.

    ``source`` records which ingest path produced this row: ``'cli'``
    for ``evalguard push`` or ``'otlp'`` for OTLP-trace synthesis.
    The UI surfaces it as a badge.
    """
    run_id = payload["run_id"]
    conn.execute(
        text("""INSERT INTO runs(
                  run_id, project_id, project_name, config_hash,
                  status, row_status, gate_status,
                  started_at, finished_at,
                  cost_usd, row_count, row_pass_count, row_fail_count,
                  payload_json, ingested_at, ingested_by, source)
                VALUES (
                  :run_id, :project_id, :project_name, :config_hash,
                  :status, :row_status, :gate_status,
                  :started_at, :finished_at,
                  :cost_usd, :row_count, :row_pass_count, :row_fail_count,
                  :payload_json, :ingested_at, :ingested_by, :source)"""),
        {
            "run_id":         run_id,
            "project_id":     project_id,
            "project_name":   payload["project"],
            "config_hash":    payload.get("config_hash"),
            "status":         payload.get("status"),
            "row_status":     payload.get("row_status"),
            "gate_status":    payload.get("gate_status"),
            "started_at":     payload.get("started_at"),
            "finished_at":    payload.get("finished_at"),
            "cost_usd":       float(payload.get("cost_usd") or 0.0),
            "row_count":      int(payload.get("row_count") or 0),
            "row_pass_count": int(payload.get("row_pass_count") or 0),
            "row_fail_count": int(payload.get("row_fail_count") or 0),
            "payload_json":   json.dumps(payload, default=str),
            "ingested_at":    now_iso(),
            "ingested_by":    principal.key_id,
            "source":         source,
        },
    )

    for asset in payload.get("assets") or []:
        conn.execute(
            text("""INSERT INTO assets(run_id, project_id, kind, asset_id, version_id, source)
                    VALUES (:run_id, :project_id, :kind, :asset_id, :version_id, :source)"""),
            {
                "run_id":     run_id,
                "project_id": project_id,
                "kind":       asset["kind"],
                "asset_id":   asset["asset_id"],
                "version_id": asset["version_id"],
                "source":     asset.get("source"),
            },
        )

    for trial in payload.get("trials") or []:
        conn.execute(
            text("""INSERT INTO trials(
                      trial_id, run_id, project_id,
                      provider_id, provider, model,
                      prompt_id, prompt_version_id,
                      row_count, row_pass_count, row_fail_count, cost_usd,
                      status, gate_status, started_at, finished_at)
                    VALUES (
                      :trial_id, :run_id, :project_id,
                      :provider_id, :provider, :model,
                      :prompt_id, :prompt_version_id,
                      :row_count, :row_pass_count, :row_fail_count, :cost_usd,
                      :status, :gate_status, :started_at, :finished_at)"""),
            {
                "trial_id":          trial["trial_id"],
                "run_id":            run_id,
                "project_id":        project_id,
                "provider_id":       trial.get("provider_id"),
                "provider":          trial.get("provider"),
                "model":             trial.get("model"),
                "prompt_id":         trial.get("prompt_id"),
                "prompt_version_id": trial.get("prompt_version_id"),
                "row_count":         int(trial.get("row_count") or 0),
                "row_pass_count":    int(trial.get("row_pass_count") or 0),
                "row_fail_count":    int(trial.get("row_fail_count") or 0),
                "cost_usd":          float(trial.get("cost_usd") or 0.0),
                "status":            trial.get("status"),
                "gate_status":       trial.get("gate_status"),
                "started_at":        trial.get("started_at"),
                "finished_at":       trial.get("finished_at"),
            },
        )
        for r in trial.get("rows") or []:
            conn.execute(
                text("""INSERT INTO run_rows(
                          run_id, trial_id, project_id, row_id,
                          passed, n_scores, cost_usd, latency_ms, cache_hit, tags_json)
                        VALUES (
                          :run_id, :trial_id, :project_id, :row_id,
                          :passed, :n_scores, :cost_usd, :latency_ms, :cache_hit, :tags_json)"""),
                {
                    "run_id":     run_id,
                    "trial_id":   trial["trial_id"],
                    "project_id": project_id,
                    "row_id":     r["row_id"],
                    "passed":     1 if r.get("passed") else 0,
                    "n_scores":   int(r.get("n_scores") or 0),
                    "cost_usd":   float(r.get("cost_usd") or 0.0),
                    "latency_ms": int(r.get("latency_ms") or 0),
                    "cache_hit":  1 if r.get("cache_hit") else 0,
                    "tags_json":  json.dumps(r.get("tags") or []),
                },
            )
        for g in trial.get("gates") or []:
            conn.execute(
                text("""INSERT INTO gate_results(
                          run_id, trial_id, project_id,
                          gate_name, blocking, passed, severity, layer, details_json)
                        VALUES (
                          :run_id, :trial_id, :project_id,
                          :gate_name, :blocking, :passed, :severity, :layer, :details_json)"""),
                {
                    "run_id":       run_id,
                    "trial_id":     trial["trial_id"],
                    "project_id":   project_id,
                    "gate_name":    g["gate_name"],
                    "blocking":     1 if g.get("blocking") else 0,
                    "passed":       1 if g["passed"] else 0,
                    "severity":     g.get("severity"),
                    "layer":        g.get("layer"),
                    "details_json": json.dumps(g.get("details") or []),
                },
            )

    aggregate = payload.get("aggregate") or {}
    for g in aggregate.get("gates") or []:
        conn.execute(
            text("""INSERT INTO gate_results(
                      run_id, trial_id, project_id,
                      gate_name, blocking, passed, severity, layer, details_json)
                    VALUES (
                      :run_id, NULL, :project_id,
                      :gate_name, :blocking, :passed, :severity, :layer, :details_json)"""),
            {
                "run_id":       run_id,
                "project_id":   project_id,
                "gate_name":    g["gate_name"],
                "blocking":     1 if g.get("blocking") else 0,
                "passed":       1 if g["passed"] else 0,
                "severity":     g.get("severity"),
                "layer":        g.get("layer"),
                "details_json": json.dumps(g.get("details") or []),
            },
        )

    audit = payload.get("audit") or {}
    if audit.get("events"):
        conn.execute(
            text("""INSERT INTO events(run_id, project_id, event_count, chain_tip, events_json)
                    VALUES (:run_id, :project_id, :event_count, :chain_tip, :events_json)"""),
            {
                "run_id":      run_id,
                "project_id":  project_id,
                "event_count": int(audit.get("event_count") or 0),
                "chain_tip":   audit.get("chain_tip"),
                "events_json": json.dumps(audit["events"], default=str),
            },
        )
