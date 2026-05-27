"""``GET /v1/projects/{slug}/calls`` — Phase OBS-1 stream view.

A "call" here is one ``run_rows`` entry — what an operator running a
customer-service deployment thinks of as one LLM interaction.  This
endpoint replaces "scroll through runs, expand a trial, find a row"
with "scroll through the project's last N calls directly".

Two tabs:

- ``tab=recent``   — newest wall-clock first.  Operator-default.
- ``tab=failures`` — same order but filtered to ``passed=0`` rows.
                     Triage-friendly.

Pagination is cursor-based (opaque base64-encoded
``{ingested_at, id}`` pair).  Cursor opacity is deliberate:

- Clients never construct cursors from scratch — only by passing
  the prior page's ``next_cursor`` back.
- A corrupted cursor returns 400, never silently dropping to
  page-one.  A buggy client paginating with a bad cursor would
  otherwise loop forever reading the same first page.

Tenant scoping mirrors ``/v1/runs/{id}``: the project lookup is the
gate.  A non-admin caller's ``org_id`` must own the project (admins
see any).  Cross-org / missing slug → 404 (no enumeration leak).
"""

from __future__ import annotations

import base64
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import get_project_by_slug
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    CallDetail, CallListResponse, CallSummary,
)


router = APIRouter()


# Same whitelist the runs filter uses.  Inlined rather than imported
# to avoid a circular import — both files are routes.
_KNOWN_SOURCES: frozenset[str] = frozenset({"cli", "otlp"})


def _encode_cursor(ingested_at: str | None, row_id: int) -> str:
    """Opaque base64-URL cursor.  Format is intentionally not
    documented to clients — they should treat it as a black-box
    token.  ``t`` (the ISO timestamp) is optional: when the last row
    of a page has a NULL ``ingested_at`` (pre-OBS-1 legacy data the
    backfill couldn't fix), we emit an id-only cursor and the next
    page WHERE-clause falls back to ``id < :cur_id``.  Without this
    fallback the stream silently terminates the first time the
    cursor lands on a NULL-timestamped row."""
    payload: dict = {"i": row_id}
    if ingested_at is not None:
        payload["t"] = ingested_at
    return base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8"),
    ).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str | None, int]:
    """Decode a cursor.  Raises ``HTTPException(400)`` on tampered
    or malformed input — see the module docstring for why we don't
    silently drop to page-one.  Returns ``(None, id)`` for legacy
    rows where the encoder couldn't supply a timestamp."""
    try:
        # Re-pad — ``rstrip("=")`` in the encoder means the decoder
        # has to add it back to a multiple of 4.
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        ts = data.get("t")
        return (str(ts) if ts is not None else None), int(data["i"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor; pass the ``next_cursor`` from the previous page.",
        ) from e


@router.get(
    "/v1/projects/{project_slug}/calls",
    response_model=CallListResponse,
    tags=["calls"],
)
def list_project_calls(
    project_slug: str,
    tab:    Literal["recent", "failures"] = Query("recent"),
    cursor: str | None = Query(default=None,
        description="Opaque cursor from the previous page's ``next_cursor``.  Omit for the first page."),
    limit:  int = Query(default=50, ge=1, le=200),
    source: str | None = Query(default=None,
        description="Optional filter: ``cli`` (pushed via ``evalguard push``) "
                    "or ``otlp`` (synthesised from a posted OTLP trace)."),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> CallListResponse:
    """Cursor-paginated stream of one project's calls, newest-first."""
    if source is not None:
        # Normalise to lowercase + trim BEFORE the whitelist check so
        # ``?source=CLI`` matches ``?source=cli``.  The sibling
        # endpoint in ``runs.py:list_runs`` does the same; consistency
        # matters because the calls + runs lists are linked from the
        # same UI tab.
        source = source.strip().lower()
        if source not in _KNOWN_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown source {source!r}. Allowed: {sorted(_KNOWN_SOURCES)}.",
            )

    # Project visibility — admin sees any project, member must own it.
    # Same anti-enumeration 404 used elsewhere; never surface "exists
    # in another org" via a 403.
    target_org = principal.org_id
    project = get_project_by_slug(conn, org_id=target_org, slug=project_slug)
    if project is None and principal.is_admin:
        # Admin fallback: search across orgs by slug (we still scope by
        # slug, not by id, to avoid leaking a non-admin's project_id).
        row = conn.execute(
            # ``slug`` is unique per-org, NOT globally, so two orgs can
            # both own a ``demo`` project.  ``ORDER BY created_at,
            # project_id`` makes the admin-fallback pick deterministic
            # (oldest first) instead of whatever the planner returns —
            # an admin hitting an ambiguous slug at least gets a stable,
            # reproducible result.  (A future admin ``?org_id=`` param
            # would disambiguate explicitly.)
            text("SELECT * FROM projects WHERE slug = :slug "
                 "ORDER BY created_at, project_id LIMIT 1"),
            {"slug": project_slug},
        ).mappings().fetchone()
        project = dict(row) if row else None
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_slug!r} not found.",
        )
    project_id = project["project_id"]

    # Build the WHERE clause incrementally.  Index used:
    # ``idx_run_rows_calls(project_id, ingested_at, id)`` — the
    # project_id eq + ingested_at range + tiebreaker on id let the
    # planner do an index-only seek for the first N rows.  Same
    # plan on SQLite and Postgres.
    clauses: list[str] = ["project_id = :project_id"]
    params: dict = {"project_id": project_id, "limit": limit}

    if tab == "failures":
        clauses.append("passed = 0")

    if source is not None:
        # ``run_rows`` doesn't carry ``source``; join through ``runs``.
        # Cheap because ``run_id`` is indexed and the source column
        # has ``idx_runs_source``.
        clauses.append(
            "run_id IN (SELECT run_id FROM runs WHERE source = :source)"
        )
        params["source"] = source

    if cursor is not None:
        cur_ts, cur_id = _decode_cursor(cursor)
        # Seek-after pagination: the next page is everything strictly
        # before ``(cur_ts, cur_id)`` in the ``(ingested_at DESC, id
        # DESC)`` order.  Row-value tuple comparisons aren't portable
        # to old SQLite, so we expand the lexicographic test manually.
        #
        # Legacy fallback: when ``cur_ts`` is None we're paginating
        # through pre-OBS-1 rows whose ``ingested_at`` is NULL.
        # Order them by ``id DESC`` alone; the ``ingested_at IS NULL``
        # guard keeps us on the legacy side until those rows are
        # exhausted (callers can then re-ingest to backfill timestamps).
        if cur_ts is None:
            clauses.append("ingested_at IS NULL AND id < :cur_id")
            params["cur_id"] = cur_id
        else:
            clauses.append(
                "(ingested_at < :cur_ts "
                " OR (ingested_at = :cur_ts AND id < :cur_id))"
            )
            params["cur_ts"] = cur_ts
            params["cur_id"] = cur_id

    where = " AND ".join(clauses)

    # ``LIMIT N + 1`` so we can tell whether a next page exists
    # without a second COUNT(*) query.  If we get N+1 rows back, the
    # next page starts after row N; otherwise the next_cursor is None.
    sql = text(f"""
        SELECT id, run_id, row_id, trial_id, project_id,
               passed, n_scores, cost_usd, latency_ms, cache_hit,
               tags_json, ingested_at, output_preview
        FROM run_rows
        WHERE {where}
        ORDER BY ingested_at DESC, id DESC
        LIMIT :limit_plus_one
    """)
    fetched = conn.execute(
        sql, {**params, "limit_plus_one": limit + 1},
    ).mappings().fetchall()

    has_more = len(fetched) > limit
    page = fetched[:limit]

    calls = [
        CallSummary(
            run_id=r["run_id"],
            row_id=r["row_id"],
            trial_id=r["trial_id"],
            project_id=r["project_id"],
            passed=bool(r["passed"]),
            cost_usd=float(r["cost_usd"] or 0.0),
            latency_ms=int(r["latency_ms"] or 0),
            cache_hit=bool(r["cache_hit"]),
            tags=_safe_json_list(r["tags_json"]),
            ingested_at=r["ingested_at"],
            output_preview=r["output_preview"],
        )
        for r in page
    ]

    next_cursor: str | None = None
    if has_more and page:
        last = page[-1]
        # ``ingested_at`` can be NULL for pre-OBS-1 rows where the
        # 0007 backfill couldn't find a parent ``runs.ingested_at``.
        # The encoder accepts that and emits an id-only cursor; the
        # WHERE clause above handles the NULL branch by ordering on
        # ``id`` alone within the NULL partition.  Without this the
        # stream silently terminated at the first legacy row.
        next_cursor = _encode_cursor(last["ingested_at"], last["id"])

    return CallListResponse(calls=calls, next_cursor=next_cursor)


def _safe_json_list(raw: str | None) -> list[str]:
    """Decode the ``tags_json`` column.  Returns ``[]`` on any
    decode failure rather than raising — a malformed tag list
    shouldn't break the whole listing."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/calls/{run_id}/{row_id}  (OBS-2)


@router.get(
    "/v1/projects/{project_slug}/calls/{run_id}/{row_id}",
    response_model=CallDetail,
    tags=["calls"],
)
def get_call_detail(
    project_slug: str,
    run_id:       str,
    row_id:       str,
    trial_id:  str | None = Query(default=None,
        description="Disambiguate when a multi-trial run evaluates the "
                    "same row_id under several providers.  Omit for "
                    "single-trial runs (returns the first match)."),
    conn:      Connection = Depends(get_conn),
    principal: Principal  = Depends(require_principal),
) -> CallDetail:
    """One call's full content — input / expected / output / scores
    plus the trial's gate verdicts as context.

    Source of truth is ``runs.payload_json`` — re-parsed at request
    time so the heavy fields stay out of the stream paginator's hot
    path.  Cross-org / missing combos all collapse to 404 (anti-
    enumeration shape used elsewhere).

    Multi-trial runs evaluate the SAME dataset ``row_id`` under
    several providers, so ``(run_id, row_id)`` alone is ambiguous.
    The stream's ``CallSummary`` carries ``trial_id``; the UI passes
    it back via ``?trial_id=`` so the detail panel shows the call
    the user actually clicked.  Without it we fall back to the first
    matching row (correct for the common single-trial case).
    """
    # Verify project + cross-org gate in one query: the project must
    # exist, the run must belong to it, AND (for non-admin) the
    # project must be in the caller's org.  Same JOIN ``get_run``
    # uses, plus the project slug check so a foreign-org slug never
    # leaks an existence signal.
    row = conn.execute(
        text("""SELECT runs.payload_json,
                       runs.ingested_at,
                       runs.project_id,
                       projects.slug   AS project_slug,
                       projects.org_id AS owning_org_id
                FROM runs
                JOIN projects ON projects.project_id = runs.project_id
                WHERE runs.run_id = :run_id AND projects.slug = :slug"""),
        {"run_id": run_id, "slug": project_slug},
    ).mappings().fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call {run_id}/{row_id!r} not found in project {project_slug!r}.",
        )
    if not principal.is_admin and row["owning_org_id"] != principal.org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call {run_id}/{row_id!r} not found in project {project_slug!r}.",
        )

    payload = json.loads(row["payload_json"])
    # Walk trials → rows looking for the requested row_id.  Linear
    # scan, but the per-trial cap is 50000 rows and the typical run
    # has < 1000 — fine for a single GET.  If this ever becomes hot
    # we'd add (run_id, row_id) → trial_id pre-resolution to
    # ``run_rows`` (the trial_id is already there).
    #
    # When ``trial_id`` is supplied (multi-trial run), skip trials
    # that don't match so we return the specific call the user
    # clicked, not trials[0]'s answer for the same dataset row.
    target_trial: dict | None = None
    target_row:   dict | None = None
    for trial in payload.get("trials") or []:
        if trial_id is not None and trial.get("trial_id") != trial_id:
            continue
        for r in trial.get("rows") or []:
            if r.get("row_id") == row_id:
                target_trial = trial
                target_row   = r
                break
        if target_row is not None:
            break
    if target_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call {run_id}/{row_id!r} not found in project {project_slug!r}.",
        )

    return CallDetail(
        run_id=run_id,
        row_id=row_id,
        trial_id=(target_trial or {}).get("trial_id"),
        project_id=row["project_id"],
        project=payload.get("project", ""),
        ingested_at=row["ingested_at"],
        # ``provider`` / ``model`` denormalised from the parent trial
        # so the UI's detail panel renders without a second fetch.
        provider=(target_trial or {}).get("provider"),
        model=(target_trial or {}).get("model"),
        passed=bool(target_row.get("passed")),
        n_scores=int(target_row.get("n_scores") or 0),
        cost_usd=float(target_row.get("cost_usd") or 0.0),
        latency_ms=int(target_row.get("latency_ms") or 0),
        cache_hit=bool(target_row.get("cache_hit")),
        tags=target_row.get("tags") or [],
        input=target_row.get("input"),
        expected=target_row.get("expected"),
        output=target_row.get("output"),
        scores=target_row.get("scores") or [],
        trial_gates=(target_trial or {}).get("gates") or [],
    )
