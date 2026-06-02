"""``/v1/projects/{slug}/config*`` — Phase PROXY-1 server-side project configs.

Three surfaces:

- ``POST /v1/projects/{slug}/config`` — upload an ``evalguard.yaml``
  blob.  Content-addressed by SHA-256: re-pushing identical bytes
  returns the existing record (200), a new revision lands as 201.
- ``GET  /v1/projects/{slug}/config`` — fetch the *latest* config for
  the project.  404 when none has ever been pushed.
- ``GET  /v1/projects/{slug}/config/history`` — list recent revisions,
  newest-first, content omitted (use the per-id GET to fetch a
  specific revision's bytes).
- ``GET  /v1/projects/{slug}/config/{config_id}`` — fetch one
  specific revision verbatim.  Used by the upcoming proxy invoke
  path when a caller pins to a specific config hash.

The server computes the SHA-256 itself rather than trusting a
client-supplied hash — the row's ``content_sha256`` is the integrity
guarantee, not a piece of caller-supplied metadata.

Tenant scoping uses the same anti-enumeration 404 shape as
``/v1/golden/*``: a cross-org or missing slug collapses to a single
404 with a uniform detail.
"""

from __future__ import annotations

import hashlib

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import now_iso, resolve_project_or_404
from evalguard_api.deps import get_conn
from evalguard_api.models import (
    ProjectConfig, ProjectConfigHistory, ProjectConfigIngest,
    ProjectConfigSummary,
)


router = APIRouter()


# Hard cap on how many history rows the list endpoint returns.  A
# project that's been pushed daily for two years still fits.
_HISTORY_MAX = 500


# Project-resolution + cross-org 404 lives in ``db.py:resolve_project_or_404``
# so all four route files share one implementation.  See that
# function's docstring for the anti-enumeration semantics.
_resolve_project = resolve_project_or_404


# ---------------------------------------------------------------------------
# POST /v1/projects/{slug}/config


@router.post(
    "/v1/projects/{project_slug}/config",
    response_model=ProjectConfig,
    tags=["configs"],
)
def push_config(
    project_slug: str,
    body: ProjectConfigIngest,
    response: Response,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfig:
    """Upload an ``evalguard.yaml`` blob.

    Content-addressed: the server computes SHA-256 over the bytes and
    UPSERTs on ``(project_id, content_sha256)``.  Idempotent re-push
    of identical bytes returns the existing record with HTTP 200; a
    new revision lands as HTTP 201.

    Returns the canonical record (id + computed hash + stored pushed_at)
    so the client can pin to a specific revision later without
    re-uploading.

    Round-4 review-pass: validate the YAML's proxy-essential shape
    at push time (must parse, must have ``version``, ``project``,
    ``providers`` with at least one entry carrying a string ``id``).
    Without this, malformed configs land in the store and surface
    only at the first ``/invoke`` call — too late for the operator
    to know their push was broken.  Deliberately a lighter check
    than the full ``packages/schemas/evalguard.schema.json``
    validation: that schema requires ``datasets`` which a pure
    proxy config legitimately doesn't carry.
    """
    project = _resolve_project(conn, principal, project_slug)
    project_id = project["project_id"]

    _validate_proxy_essential_shape(body.content)

    sha = hashlib.sha256(body.content.encode("utf-8")).hexdigest()

    # Idempotency: if a row already exists for (project_id, sha256),
    # return it as 200 — same bytes, same revision, no new row.
    existing = conn.execute(
        text("""SELECT id, pushed_by, pushed_at
                FROM project_configs
                WHERE project_id = :project_id
                  AND content_sha256 = :sha
                LIMIT 1"""),
        {"project_id": project_id, "sha": sha},
    ).mappings().fetchone()
    if existing:
        response.status_code = status.HTTP_200_OK
        return ProjectConfig(
            id=existing["id"],
            project_id=project_id,
            content_sha256=sha,
            content=body.content,
            pushed_by=existing["pushed_by"],
            pushed_at=existing["pushed_at"],
        )

    now = now_iso()
    try:
        with conn.begin_nested():
            conn.execute(
                text("""INSERT INTO project_configs(
                          project_id, content_sha256, content,
                          pushed_by, pushed_at)
                        VALUES (:project_id, :sha, :content,
                                :pusher, :now)"""),
                {"project_id": project_id, "sha": sha,
                 "content": body.content, "pusher": principal.key_id,
                 "now": now},
            )
    except IntegrityError:
        # Concurrent push of the same bytes won the race.  Re-SELECT
        # and return as 200 (matches the idempotency contract above).
        existing = conn.execute(
            text("""SELECT id, pushed_by, pushed_at
                    FROM project_configs
                    WHERE project_id = :project_id
                      AND content_sha256 = :sha
                    LIMIT 1"""),
            {"project_id": project_id, "sha": sha},
        ).mappings().fetchone()
        if existing is None:
            raise   # unique-constraint fired without a row — surface as 500
        response.status_code = status.HTTP_200_OK
        return ProjectConfig(
            id=existing["id"],
            project_id=project_id,
            content_sha256=sha,
            content=body.content,
            pushed_by=existing["pushed_by"],
            pushed_at=existing["pushed_at"],
        )

    # Successful INSERT — re-SELECT for the canonical id (lastrowid
    # is None on Postgres).
    row = conn.execute(
        text("""SELECT id FROM project_configs
                WHERE project_id = :project_id
                  AND content_sha256 = :sha
                LIMIT 1"""),
        {"project_id": project_id, "sha": sha},
    ).mappings().fetchone()
    assert row is not None, "row vanished immediately after INSERT"

    response.status_code = status.HTTP_201_CREATED
    return ProjectConfig(
        id=row["id"],
        project_id=project_id,
        content_sha256=sha,
        content=body.content,
        pushed_by=principal.key_id,
        pushed_at=now,
    )


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/config


@router.get(
    "/v1/projects/{project_slug}/config",
    response_model=ProjectConfig,
    tags=["configs"],
)
def get_latest_config(
    project_slug: str,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfig:
    """Fetch the latest config revision for the project.  404 if the
    project exists but no config has been pushed yet."""
    project = _resolve_project(conn, principal, project_slug)

    row = conn.execute(
        text("""SELECT id, project_id, content_sha256, content,
                       pushed_by, pushed_at
                FROM project_configs
                WHERE project_id = :project_id
                ORDER BY pushed_at DESC, id DESC
                LIMIT 1"""),
        {"project_id": project["project_id"]},
    ).mappings().fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config has been pushed for project {project_slug!r}.",
        )
    return ProjectConfig(**dict(row))


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/config/history


@router.get(
    "/v1/projects/{project_slug}/config/history",
    response_model=ProjectConfigHistory,
    tags=["configs"],
)
def list_config_history(
    project_slug: str,
    limit: int = Query(default=20, ge=1, le=_HISTORY_MAX),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfigHistory:
    """List recent config revisions for the project, newest-first.
    ``content`` is omitted from each entry — fetch a specific
    revision's bytes via the per-id GET below."""
    project = _resolve_project(conn, principal, project_slug)

    rows = conn.execute(
        text("""SELECT id, project_id, content_sha256, pushed_by, pushed_at
                FROM project_configs
                WHERE project_id = :project_id
                ORDER BY pushed_at DESC, id DESC
                LIMIT :limit"""),
        {"project_id": project["project_id"], "limit": limit},
    ).mappings().fetchall()
    return ProjectConfigHistory(
        configs=[ProjectConfigSummary(**dict(r)) for r in rows],
    )


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/config/{config_id}


@router.get(
    "/v1/projects/{project_slug}/config/{config_id}",
    response_model=ProjectConfig,
    tags=["configs"],
)
def get_config_revision(
    project_slug: str,
    config_id: int,
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> ProjectConfig:
    """Fetch one specific revision verbatim.  The id+slug pair must
    match — passing another project's id under this slug surfaces as
    a 404 to match the anti-enumeration shape."""
    project = _resolve_project(conn, principal, project_slug)

    row = conn.execute(
        text("""SELECT id, project_id, content_sha256, content,
                       pushed_by, pushed_at
                FROM project_configs
                WHERE id = :id AND project_id = :project_id"""),
        {"id": config_id, "project_id": project["project_id"]},
    ).mappings().fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config revision {config_id} not found for project {project_slug!r}.",
        )
    return ProjectConfig(**dict(row))


# ---------------------------------------------------------------------------
# Push-time validation (round-4 review-pass — fail fast at push, not
# at first invoke).


def _validate_proxy_essential_shape(content: str) -> None:
    """Raise ``HTTPException(422)`` if the YAML is unparseable or
    missing the fields the proxy will read at ``/invoke`` time.

    Lighter than full ``evalguard.schema.json`` validation: the
    schema requires ``datasets``, but a pure-proxy config has no
    dataset (production traffic is the input).  We check only what
    the proxy actually consumes:

    - parses as YAML mapping (the existing 422 at ``_load_latest_config``
      would also catch this, but we move it earlier so the OPERATOR
      knows their push was broken)
    - ``version`` is set (drift canary — schema bumps would tighten this)
    - ``project`` is a non-empty string (matches the slug semantic)
    - ``providers`` is a non-empty list, each entry has a string ``id``
      (the proxy picks ``providers[0]``; an empty list or a malformed
      entry would 422 at invoke time forever).
    """
    try:
        cfg = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Config is not valid YAML: {e}",
        ) from None
    if not isinstance(cfg, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Config must be a YAML mapping at the top level.",
        )
    if "version" not in cfg:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Config must declare a top-level ``version`` field.",
        )
    project = cfg.get("project")
    if not isinstance(project, str) or not project.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Config must declare a non-empty string ``project`` field.",
        )
    providers = cfg.get("providers")
    if not isinstance(providers, list) or not providers:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Config must declare a non-empty ``providers`` list.",
        )
    for i, entry in enumerate(providers):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"providers[{i}] must be a mapping (got {type(entry).__name__}).",
            )
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"providers[{i}] must carry a non-empty string ``id``.",
            )
        # Round-4 ultra-review (Agent-2 G): the proxy reads
        # ``providers[i].config`` and passes it to ``load_provider``;
        # if a scalar slips through here it'd 500 the invoke call
        # with an opaque ``AttributeError: 'str' has no attribute
        # 'get'``.  Catch it at push so the operator sees the typo.
        pcfg = entry.get("config")
        if pcfg is not None and not isinstance(pcfg, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"providers[{i}].config must be a YAML mapping if set "
                    f"(got {type(pcfg).__name__})."
                ),
            )

    # Round-4 ultra-review (Agent-3 F): the quota fields the proxy
    # reads at /invoke time MUST be sane numbers, otherwise an
    # operator's typo silently disables the protection.  Specifically:
    # - ``rate_limit_per_minute: -1`` would pass ``≤ 0 → disabled``
    #   in quotas.py.  Reject negative + non-int values at push.
    # - ``cost_cap_usd_daily: NaN`` would make every ``NaN > 0`` /
    #   ``today_cost_usd >= NaN`` comparison False, silently bypassing
    #   the cap.  Reject non-finite values.
    rl = cfg.get("rate_limit_per_minute")
    if rl is not None:
        if isinstance(rl, bool) or not isinstance(rl, int) or rl < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "``rate_limit_per_minute`` must be a non-negative integer "
                    "(0 disables the per-key rate limit; any positive value "
                    "caps requests/minute per API key)."
                ),
            )
    cap = cfg.get("cost_cap_usd_daily")
    if cap is not None:
        import math
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "``cost_cap_usd_daily`` must be a non-negative finite number "
                    "(0 disables the daily cost cap)."
                ),
            )

    # Operator-typo guard (Agent-3 K): the CLI executor's config
    # uses ``cost_cap_usd`` (per-run abort), the proxy uses
    # ``cost_cap_usd_daily`` (per-day budget).  An operator who
    # writes the wrong field gets no warning and no protection.
    # Emit a structured log + warning so it surfaces in the access
    # log without 422'ing (the field is legal for the batch path).
    if "cost_cap_usd" in cfg and "cost_cap_usd_daily" not in cfg:
        import logging as _logging
        _logging.getLogger("evalguard.api.configs").warning(
            '{"evt":"config.typo_warn","field_set":"cost_cap_usd",'
            '"field_expected":"cost_cap_usd_daily",'
            '"hint":"cost_cap_usd is the CLI executor field (per-run abort); '
            'the proxy reads cost_cap_usd_daily (per-day budget)."}',
        )
