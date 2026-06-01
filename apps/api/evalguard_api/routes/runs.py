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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import now_iso, upsert_project
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    DriftMetric, DriftReport, RunIngest, RunList, RunOut, RunSummary,
)
from evalguard_api.stats import welchs_t_test

router = APIRouter()


# OBS-1: cap on the denormalised ``run_rows.output_preview`` snippet.
# 240 characters is enough for ~2 lines in the stream-view card without
# the column getting fat in the on-disk row.  Full output remains
# available via ``GET /v1/runs/{id}`` and the per-call detail endpoint.
_PREVIEW_CHARS: int = 240


# ---------------------------------------------------------------------------
# POST /v1/runs


@router.post("/v1/runs", status_code=status.HTTP_201_CREATED, tags=["runs"])
def ingest_run(
    body: RunIngest,
    request: Request,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    # The run lands in the *caller's* org — no client-supplied org
    # parameter, by design. The payload's ``project: <name>`` becomes
    # the project slug within that org.
    project_id = upsert_project(
        conn, org_id=principal.org_id, project_name=body.project,
    )

    # Idempotency: a CI re-run that pushes the same run_id should not
    # 409 — return the existing resource as 200 OK so the caller sees
    # success and moves on. The CLI sends ``Idempotency-Key: <run_id>``
    # so the contract is explicit. Without the header, fall back to
    # the previous strict 409 behaviour (a duplicate by accident is
    # likely a bug we want surfaced loudly).
    #
    # Scope the existence check to the caller's project — on SQLite
    # there is no RLS to filter cross-org rows, and an unscoped lookup
    # would falsely report a cross-org run_id as an idempotent replay
    # of *this* org's project. Scoping the SELECT means cross-org
    # collisions instead fall through to the INSERT and hit the PK
    # violation handler below.
    idem_key = request.headers.get("idempotency-key")
    existing = conn.execute(
        text("SELECT run_id FROM runs "
             "WHERE run_id=:run_id AND project_id=:project_id"),
        {"run_id": body.run_id, "project_id": project_id},
    ).fetchone()
    if existing:
        if idem_key == body.run_id:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "run_id":     body.run_id,
                    "url":        f"/v1/runs/{body.run_id}",
                    "project_id": project_id,
                    "idempotent_replay": True,
                },
                headers={"Location": f"/v1/runs/{body.run_id}"},
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {body.run_id!r} already exists. Send "
                f"``Idempotency-Key: {body.run_id}`` if this is a "
                f"deliberate retry."
            ),
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
    try:
        _persist_run(conn, payload, project_id, principal)
    except IntegrityError:
        # Cross-org run_id collision (the project-scoped SELECT above
        # didn't see the row, but the global PK on ``runs.run_id`` does)
        # or a race between two concurrent ingests racing the same id.
        # Surface as a generic 409 — the message is identical to the
        # in-org duplicate path so cross-org existence isn't leaked.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {body.run_id!r} already exists. Send "
                f"``Idempotency-Key: {body.run_id}`` if this is a "
                f"deliberate retry."
            ),
        ) from None

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


# Whitelist for the ``?source=`` filter on ``GET /v1/runs``.  The
# column itself accepts any string (server_default=``'cli'``), but
# the public surface is constrained so an unknown value 400s rather
# than silently returning zero rows.  Matches the ingest paths the
# server actually understands today: ``cli`` (push), ``otlp``
# (traces), and ``live`` (Phase PROXY-2 ``POST /v1/projects/{slug}/invoke``).
_KNOWN_SOURCES: frozenset[str] = frozenset({"cli", "otlp", "live"})


@router.get("/v1/runs", response_model=RunList, tags=["runs"])
def list_runs(
    project: str | None = Query(default=None, description="Filter by project slug."),
    source:  str | None = Query(default=None,
        description="Filter by ingest source: ``cli`` (pushed via ``evalguard push``) "
                    "or ``otlp`` (synthesised from a posted OTLP trace)."),
    limit: int = Query(default=20, ge=1, le=200),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> RunList:
    """List recent runs visible to the caller.

    Org members see only runs in their own org; admins see every
    run unless ``project`` narrows the listing further. The org
    filter is implicit so a member can never enumerate a foreign
    org's runs by listing without a filter.

    ``source`` is the Phase-3a ingest-path discriminator.  Unknown
    values 400 — the public surface is ``cli`` / ``otlp`` only.
    """
    # Normalise to lowercase BEFORE the whitelist check so an
    # operator who guesses ``?source=CLI`` or ``OTLP`` gets a
    # matching result instead of a confusing 400.  The column is
    # stamped lowercase at ingest (``_persist_run`` / OTLP route),
    # so we can also use the normalised value as the bind param.
    if source is not None:
        source = source.strip().lower()
        if source not in _KNOWN_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown source {source!r}. Allowed: {sorted(_KNOWN_SOURCES)}.",
            )

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
    if source is not None:
        clauses.append("source = :source")
        params["source"] = source
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
    # E.4 — recompute ``audit.event_count`` from ``len(events)`` on
    # read instead of trusting whatever the client wrote at ingest.
    # The chain hash is the actual tamper-evidence; the count is for
    # operator quick-look. Recomputing here means a malicious client
    # that lied about the count can't surface that lie via GET.
    audit = payload.get("audit")
    if isinstance(audit, dict):
        events = audit.get("events")
        if isinstance(events, list):
            audit["event_count"] = len(events)
    return RunOut.model_validate(payload)


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/drift?vs=<baseline_id>


# Per-row metrics that drift compares. Order in this tuple drives
# the metrics list in the response so the UI's per-metric table
# is stable across calls.
_DRIFT_METRICS: tuple[str, ...] = ("latency_ms", "cost_usd", "passed")


@router.get(
    "/v1/runs/{run_id}/drift",
    response_model=DriftReport,
    tags=["runs"],
)
def get_drift(
    run_id: str,
    vs:    str   = Query(..., description="Baseline run id to compare against."),
    alpha: float = Query(0.05, gt=0, lt=1,
                         description="Significance threshold for the per-metric verdict."),
    conn:  Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> DriftReport:
    """Compare two runs' per-row metric distributions.

    For each of latency_ms / cost_usd / passed, run Welch's
    two-sample t-test on the per-row values from each run.  Returns
    one ``DriftMetric`` per metric with the t-stat, p-values, and a
    single-bit ``significant_at_alpha`` flag.  Metrics that can't
    be compared (e.g. one side has < 2 rows) end up under
    ``skipped`` so the UI can render a complete report.

    Cross-tenant isolation matches ``GET /v1/runs/{id}``: a member
    asking about a run in another org gets 404 (no enumeration
    leak), regardless of which side of the comparison the foreign
    run is on.
    """
    if run_id == vs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot compute drift between a run and itself.",
        )

    # Both runs must be visible to the caller. The same JOIN as
    # ``get_run`` enforces "the run's project belongs to the
    # caller's org, or the caller is admin"; missing-or-foreign
    # collapses to a single 404 with no extra detail.
    visible_ids = _runs_visible_to(conn, principal, [run_id, vs])
    if run_id not in visible_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )
    if vs not in visible_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {vs!r} not found.",
        )

    cur_rows  = _row_metric_samples(conn, run_id)
    base_rows = _row_metric_samples(conn, vs)

    metrics:  list[DriftMetric] = []
    skipped:  list[dict] = []
    for name in _DRIFT_METRICS:
        cur_vals  = cur_rows.get(name, [])
        base_vals = base_rows.get(name, [])
        if len(cur_vals) < 2 or len(base_vals) < 2:
            skipped.append({
                "name":   name,
                "reason": f"need ≥2 samples per side; got "
                          f"current={len(cur_vals)}, baseline={len(base_vals)}",
            })
            continue
        result = welchs_t_test(cur_vals, base_vals)
        delta_mean = result.mean1 - result.mean2
        # Significance == directional change AND p < alpha. The
        # ``delta_mean != 0`` check is belt-and-suspenders for the
        # zero-variance / identical-mean edge case the helper
        # already returns p_two_sided=1.0 for.
        significant = (result.p_two_sided < alpha) and (delta_mean != 0.0)
        metrics.append(DriftMetric(
            name=name,
            n_current=result.n1, n_baseline=result.n2,
            mean_current=result.mean1, mean_baseline=result.mean2,
            delta_mean=delta_mean,
            t_stat=result.t_stat, dof=result.dof,
            p_two_sided=result.p_two_sided,
            p_less=result.p_less, p_greater=result.p_greater,
            significant_at_alpha=significant,
        ))

    return DriftReport(
        current_run_id=run_id,
        baseline_run_id=vs,
        alpha=alpha,
        metrics=metrics,
        skipped=skipped,
    )


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
    # Compute once: ``runs.ingested_at`` AND every ``run_rows.ingested_at``
    # under this run share the same value so the denormalised
    # ``run_rows.ingested_at`` (OBS-1) and the parent ``runs.ingested_at``
    # never drift apart — important because the calls-stream's
    # composite index orders by the denormalised column.
    ingested_at = now_iso()
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
            "ingested_at":    ingested_at,
            "ingested_by":    principal.key_id,
            "source":         source,
        },
    )

    assets_param = [
        {
            "run_id":     run_id,
            "project_id": project_id,
            "kind":       asset["kind"],
            "asset_id":   asset["asset_id"],
            "version_id": asset["version_id"],
            "source":     asset.get("source"),
        }
        for asset in (payload.get("assets") or [])
    ]
    if assets_param:
        conn.execute(
            text("""INSERT INTO assets(run_id, project_id, kind, asset_id, version_id, source)
                    VALUES (:run_id, :project_id, :kind, :asset_id, :version_id, :source)"""),
            assets_param,
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
        # Bulk-insert rows + per-trial gates via ``executemany`` so a
        # 50k-row run is one round-trip per insert family rather than
        # 50k. SQLAlchemy's ``execute(stmt, [dict, dict, ...])`` form
        # is the canonical idiom; on Postgres it's collapsed into a
        # multi-VALUES INSERT, on SQLite into a tight C-loop. Per-row
        # SQL latency is the dominant cost on bulk import.
        rows_param: list[dict] = []
        for r in trial.get("rows") or []:
            # ``output_preview`` denormalises a leading slice of the
            # raw output for the calls-stream UI (OBS-1).  We trim
            # at ``_PREVIEW_CHARS`` because the stream card has a
            # fixed width — the full output is still available via
            # the per-call detail endpoint.  Non-string outputs are
            # rendered via ``json.dumps`` so structured outputs
            # (tool calls, etc.) survive the trim with their shape
            # intact for a glance.
            output = r.get("output")
            if isinstance(output, str):
                preview = output[:_PREVIEW_CHARS]
            elif output is None:
                preview = None
            else:
                preview = json.dumps(output, default=str)[:_PREVIEW_CHARS]
            rows_param.append({
                "run_id":         run_id,
                "trial_id":       trial["trial_id"],
                "project_id":     project_id,
                "row_id":         r["row_id"],
                "passed":         1 if r.get("passed") else 0,
                "n_scores":       int(r.get("n_scores") or 0),
                "cost_usd":       float(r.get("cost_usd") or 0.0),
                "latency_ms":     int(r.get("latency_ms") or 0),
                "cache_hit":      1 if r.get("cache_hit") else 0,
                "tags_json":      json.dumps(r.get("tags") or []),
                # OBS-1: stamp from the same ``now_iso()`` the parent
                # ``runs`` row used so rows + parent agree to the µs.
                "ingested_at":    ingested_at,
                "output_preview": preview,
            })
        if rows_param:
            conn.execute(
                text("""INSERT INTO run_rows(
                          run_id, trial_id, project_id, row_id,
                          passed, n_scores, cost_usd, latency_ms, cache_hit, tags_json,
                          ingested_at, output_preview)
                        VALUES (
                          :run_id, :trial_id, :project_id, :row_id,
                          :passed, :n_scores, :cost_usd, :latency_ms, :cache_hit, :tags_json,
                          :ingested_at, :output_preview)"""),
                rows_param,
            )

        gates_param: list[dict] = []
        for g in trial.get("gates") or []:
            gates_param.append({
                "run_id":       run_id,
                "trial_id":     trial["trial_id"],
                "project_id":   project_id,
                "gate_name":    g["gate_name"],
                "blocking":     1 if g.get("blocking") else 0,
                "passed":       1 if g["passed"] else 0,
                "severity":     g.get("severity"),
                "layer":        g.get("layer"),
                "details_json": json.dumps(g.get("details") or []),
            })
        if gates_param:
            conn.execute(
                text("""INSERT INTO gate_results(
                          run_id, trial_id, project_id,
                          gate_name, blocking, passed, severity, layer, details_json)
                        VALUES (
                          :run_id, :trial_id, :project_id,
                          :gate_name, :blocking, :passed, :severity, :layer, :details_json)"""),
                gates_param,
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


# ---------------------------------------------------------------------------
# Drift internals


def _runs_visible_to(
    conn: Connection,
    principal: Principal,
    run_ids: list[str],
) -> set[str]:
    """Return the subset of ``run_ids`` the caller is allowed to see.

    Mirrors ``get_run``'s scoping in one round-trip so the drift
    endpoint can flag missing-or-foreign with a 404 each — no extra
    query per side.
    """
    if not run_ids:
        return set()
    # Generate ``(:r0, :r1, …)`` placeholders so each id is bound
    # individually; never f-string raw user input into SQL.
    keys   = [f"r{i}" for i in range(len(run_ids))]
    placeholders = ", ".join(f":{k}" for k in keys)
    params: dict = dict(zip(keys, run_ids))
    base = f"""
        SELECT runs.run_id, projects.org_id AS owning_org_id
        FROM runs
        JOIN projects ON projects.project_id = runs.project_id
        WHERE runs.run_id IN ({placeholders})
    """
    rows = conn.execute(text(base), params).mappings().fetchall()
    if principal.is_admin:
        return {r["run_id"] for r in rows}
    return {
        r["run_id"] for r in rows
        if r["owning_org_id"] == principal.org_id
    }


def _row_metric_samples(
    conn: Connection,
    run_id: str,
) -> dict[str, list[float]]:
    """Pull per-row metric vectors for ``run_id``.

    One pass over ``run_rows`` collects the three metrics we expose
    today (latency_ms / cost_usd / passed). Returned as float lists
    so the Welch helper can take them verbatim.
    """
    rows = conn.execute(
        text("""SELECT passed, cost_usd, latency_ms
                FROM run_rows WHERE run_id = :run_id"""),
        {"run_id": run_id},
    ).mappings().fetchall()
    return {
        "latency_ms": [float(r["latency_ms"] or 0) for r in rows],
        "cost_usd":   [float(r["cost_usd"]   or 0) for r in rows],
        # ``passed`` is stored as 0/1; treat as a Bernoulli sample so
        # the t-test gives a usable signal on pass-rate drift.
        "passed":     [float(r["passed"]) for r in rows],
    }
