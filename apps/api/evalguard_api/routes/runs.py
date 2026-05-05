"""``/v1/runs`` — ingest, list, fetch.

Three endpoints implementing the Phase-2 ``evalguard push`` target:

- ``POST /v1/runs`` — accept a run JSON, persist it, return the
  canonical URL. 409 on duplicate ``run_id`` (no implicit overwrite).
- ``GET  /v1/runs`` — list recent runs, filterable by project.
- ``GET  /v1/runs/{run_id}`` — fetch the full run JSON, with a
  server-injected envelope.

The ingestion path persists the canonical run dict verbatim under
``runs.payload_json`` AND denormalizes trial / row / gate / asset /
event tables so the eventual UI can query without parsing JSON.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import (
    connect, now_iso, transaction, upsert_project,
)
from evalguard_api.models import RunIngest, RunList, RunOut, RunSummary

router = APIRouter()


def _conn(request: Request) -> sqlite3.Connection:
    """Open a per-request connection. SQLite connection-per-request is
    the standard pattern; pooling lives one phase later when the
    Postgres port lands."""
    settings = request.app.state.settings
    path = settings.sqlite_path
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Non-sqlite DATABASE_URL not yet implemented: {settings.database_url}",
        )
    return connect(path)


# ---------------------------------------------------------------------------
# POST /v1/runs


@router.post("/v1/runs", status_code=status.HTTP_201_CREATED, tags=["runs"])
def ingest_run(
    body: RunIngest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    conn = _conn(request)
    try:
        # The run lands in the *caller's* org — there's no client-
        # supplied org parameter, by design. ``run_to_dict()``'s
        # ``project: <name>`` field becomes the project slug within
        # that org. Two orgs may host projects with identical slugs
        # without collision.
        project_id = upsert_project(
            conn, org_id=principal.org_id, project_name=body.project,
        )

        # Conflict on duplicate run_id. The canonical merge story
        # (server-side reconciliation across multiple pushes) is
        # post-MVP; today we surface 409 so the client knows.
        existing = conn.execute(
            "SELECT 1 FROM runs WHERE run_id=?", (body.run_id,),
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
        with transaction(conn):
            _persist_run(conn, payload, project_id, principal)

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "run_id": body.run_id,
                "url":    f"/v1/runs/{body.run_id}",
                "project_id": project_id,
            },
            headers={"Location": f"/v1/runs/{body.run_id}"},
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /v1/runs


@router.get("/v1/runs", response_model=RunList, tags=["runs"])
def list_runs(
    request: Request,
    project: str | None = Query(default=None, description="Filter by project slug."),
    limit: int = Query(default=20, ge=1, le=200),
    principal: Principal = Depends(require_principal),
) -> RunList:
    """List recent runs visible to the caller.

    Org members see only runs in their own org; admins see every
    run unless ``project`` narrows the listing further. The org
    filter is implicit so a member can never enumerate a foreign
    org's runs by listing without a filter.
    """
    conn = _conn(request)
    try:
        # WHERE clause assembled with parameters — never f-strings —
        # so user input can't reach the SQL surface.
        clauses: list[str] = []
        args: list = []
        if not principal.is_admin:
            clauses.append("project_id IN (SELECT project_id FROM projects WHERE org_id = ?)")
            args.append(principal.org_id)
        if project is not None:
            clauses.append("project_name = ?")
            args.append(project)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        rows = conn.execute(
            f"""SELECT run_id, project_name AS project, status, gate_status,
                       started_at, finished_at,
                       row_count, row_pass_count, row_fail_count, cost_usd,
                       ingested_at, ingested_by
                FROM runs
                {where}
                ORDER BY ingested_at DESC, rowid DESC LIMIT ?""",
            tuple(args),
        ).fetchall()
        return RunList(runs=[RunSummary(**dict(r)) for r in rows], next=None)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}


@router.get("/v1/runs/{run_id}", response_model=RunOut, tags=["runs"])
def get_run(
    run_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> RunOut:
    """Fetch a run by id.

    Cross-org reads return **404** rather than 403 — exposing the
    existence of a run in another org would let a curious caller
    enumerate run_ids across tenants. The 404 is identical to the
    "this id never existed" response.
    """
    conn = _conn(request)
    try:
        row = conn.execute(
            """SELECT runs.payload_json, runs.ingested_at, runs.ingested_by,
                      runs.project_id, projects.org_id AS owning_org_id
               FROM runs
               JOIN projects ON projects.project_id = runs.project_id
               WHERE runs.run_id=?""",
            (run_id,),
        ).fetchone()
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
        # Server-injected envelope so an operator can see how the run
        # arrived without joining tables.
        payload["server"] = {
            "ingested_at": row["ingested_at"],
            "ingested_by": row["ingested_by"],
            "project_id":  row["project_id"],
        }
        return RunOut.model_validate(payload)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal — persistence helpers


def _persist_run(
    conn: sqlite3.Connection,
    payload: dict,
    project_id: str,
    principal: Principal,
) -> None:
    """Write the run + denormalized trial / row / gate / asset / event
    rows. Caller wraps the whole thing in a transaction.

    Mirrors the CLI's ``serializer.run_to_dict`` shape 1:1 — any drift
    in input shape is caught by Pydantic upstream of this call, so
    here we trust the payload structure.
    """
    run_id = payload["run_id"]
    conn.execute(
        """INSERT INTO runs(
              run_id, project_id, project_name, config_hash,
              status, row_status, gate_status,
              started_at, finished_at,
              cost_usd, row_count, row_pass_count, row_fail_count,
              payload_json, ingested_at, ingested_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, project_id, payload["project"], payload.get("config_hash"),
            payload.get("status"), payload.get("row_status"), payload.get("gate_status"),
            payload.get("started_at"), payload.get("finished_at"),
            float(payload.get("cost_usd") or 0.0),
            int(payload.get("row_count") or 0),
            int(payload.get("row_pass_count") or 0),
            int(payload.get("row_fail_count") or 0),
            json.dumps(payload, default=str),
            now_iso(),
            principal.key_id,
        ),
    )

    for asset in payload.get("assets") or []:
        conn.execute(
            "INSERT INTO assets(run_id, project_id, kind, asset_id, version_id, source) VALUES (?,?,?,?,?,?)",
            (run_id, project_id, asset["kind"], asset["asset_id"],
             asset["version_id"], asset.get("source")),
        )

    for trial in payload.get("trials") or []:
        conn.execute(
            """INSERT INTO trials(
                  trial_id, run_id, project_id,
                  provider_id, provider, model,
                  prompt_id, prompt_version_id,
                  row_count, row_pass_count, row_fail_count, cost_usd,
                  status, gate_status, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trial["trial_id"], run_id, project_id,
                trial.get("provider_id"), trial.get("provider"), trial.get("model"),
                trial.get("prompt_id"), trial.get("prompt_version_id"),
                int(trial.get("row_count") or 0),
                int(trial.get("row_pass_count") or 0),
                int(trial.get("row_fail_count") or 0),
                float(trial.get("cost_usd") or 0.0),
                trial.get("status"), trial.get("gate_status"),
                trial.get("started_at"), trial.get("finished_at"),
            ),
        )
        for r in trial.get("rows") or []:
            conn.execute(
                """INSERT INTO run_rows(
                      run_id, trial_id, project_id, row_id,
                      passed, n_scores, cost_usd, latency_ms, cache_hit, tags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, trial["trial_id"], project_id, r["row_id"],
                    1 if r.get("passed") else 0,
                    int(r.get("n_scores") or 0),
                    float(r.get("cost_usd") or 0.0),
                    int(r.get("latency_ms") or 0),
                    1 if r.get("cache_hit") else 0,
                    json.dumps(r.get("tags") or []),
                ),
            )
        for g in trial.get("gates") or []:
            conn.execute(
                """INSERT INTO gate_results(
                      run_id, trial_id, project_id,
                      gate_name, blocking, passed, severity, layer, details_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, trial["trial_id"], project_id,
                    g["gate_name"],
                    1 if g.get("blocking") else 0,
                    1 if g["passed"] else 0,
                    g.get("severity"),
                    g.get("layer"),
                    json.dumps(g.get("details") or []),
                ),
            )

    aggregate = payload.get("aggregate") or {}
    for g in aggregate.get("gates") or []:
        conn.execute(
            """INSERT INTO gate_results(
                  run_id, trial_id, project_id,
                  gate_name, blocking, passed, severity, layer, details_json)
               VALUES (?,?,NULL,?,?,?,?,?,?)""",
            (
                run_id, project_id,
                g["gate_name"],
                1 if g.get("blocking") else 0,
                1 if g["passed"] else 0,
                g.get("severity"),
                g.get("layer"),
                json.dumps(g.get("details") or []),
            ),
        )
        # NB: trial_id NULL means "run-level / aggregate gate"; matches
        # the CLI's convention.

    audit = payload.get("audit") or {}
    if audit.get("events"):
        conn.execute(
            """INSERT INTO events(run_id, project_id, event_count, chain_tip, events_json)
               VALUES (?,?,?,?,?)""",
            (
                run_id, project_id,
                int(audit.get("event_count") or 0),
                audit.get("chain_tip"),
                json.dumps(audit["events"], default=str),
            ),
        )
