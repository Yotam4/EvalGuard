"""``/v1/projects/{slug}/audit/*`` — list + verify the proxy audit chain.

Phase PROXY-3.5.  Two surfaces:

- ``GET /v1/projects/{slug}/audit/events?run_id=...&limit=...`` —
  list emitted events for a live run in chain order.
- ``GET /v1/projects/{slug}/audit/verify?run_id=...`` — re-walk the
  chain and check every ``prev_event_hash → event_hash`` link.
  Returns ``{ok, events, broken_at, reason}`` — the exact shape the
  CLI's ``verify_chain`` returns, so an operator who learned the
  audit-verify response from one tool sees it from both.

Tenant scoping uses the same anti-enumeration 404 shape as the rest
of the API: a cross-org or missing slug both return 404.

This endpoint is read-only.  Writes happen via ``invoke.py`` →
``audit_persistence.emit_event``; there is no manual append API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.audit_persistence import count_events_for_run, list_events_for_run
from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import resolve_project_or_404
from evalguard_api.deps import get_conn
from evalguard_api.models import AuditEventList, AuditVerifyResponse
from evalguard_evaluators.audit import verify_chain_events


router = APIRouter()

# Project-resolution + cross-org 404 helper.  Same alias pattern as
# the other routes; the actual implementation lives in ``db.py``.
_resolve_project = resolve_project_or_404


# Cap on events per response.  500 is plenty for a day's live run
# (one event per /invoke call).  Operators auditing past this need
# the future ``?cursor=`` pagination — tracked for a follow-up.
_EVENTS_MAX: int = 500


def _assert_run_belongs_to_project(
    conn: Connection, run_id: str, project_id: str,
) -> None:
    """A caller can supply any ``run_id``; we must verify the run
    actually belongs to the resolved project before reading events.
    Without this gate a member who knows a foreign-org run_id could
    enumerate audit content cross-project (RLS on event_rows blocks
    the read on Postgres, but the application-layer check is the
    portable defence-in-depth)."""
    row = conn.execute(
        text("SELECT project_id FROM runs WHERE run_id = :rid"),
        {"rid": run_id},
    ).first()
    if row is None or row[0] != project_id:
        # Anti-enumeration: don't distinguish "no such run" from
        # "wrong project for run".  Both surface as 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found in this project.",
        )


@router.get(
    "/v1/projects/{project_slug}/audit/events",
    response_model=AuditEventList,
    tags=["audit"],
)
def list_audit_events(
    project_slug: str,
    run_id: str = Query(..., description="Live run to list events for."),
    limit: int = Query(default=_EVENTS_MAX, ge=1, le=_EVENTS_MAX),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> AuditEventList:
    """Return events for one live run, in chain order (id ASC ⇒
    insertion order = chain order).  The events include the full
    canonical record ``build_event`` produced; this is what
    ``verify_chain_events`` will re-hash."""
    project = _resolve_project(conn, principal, project_slug)
    _assert_run_belongs_to_project(conn, run_id, project["project_id"])
    events, corrupt = list_events_for_run(conn, run_id, limit=limit)
    # Round-5 ultra-review (Correctness G): surface corrupt-row
    # count + truncation flag explicitly.  Without the flag, a
    # caller reading /audit/events for a 50k-event chain couldn't
    # tell the response was a 500-row prefix; without
    # ``corrupt_rows``, a silently-dropped malformed event_json
    # would vanish with no signal even though /audit/verify
    # would report ``ok=False`` on the same data.
    total = count_events_for_run(conn, run_id)
    truncated = total > limit
    return AuditEventList(
        events=events,
        count=len(events),
        corrupt_rows=corrupt,
        total=total,
        truncated=truncated,
    )


@router.get(
    "/v1/projects/{project_slug}/audit/verify",
    response_model=AuditVerifyResponse,
    tags=["audit"],
)
def verify_audit_chain(
    project_slug: str,
    run_id: str = Query(..., description="Live run to verify the chain for."),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> AuditVerifyResponse:
    """Walk the per-run chain and verify every link.

    Returns the same ``{ok, events, broken_at, reason}`` shape the
    CLI's ``verify_chain`` returns — operators reading one log can
    parse the other.  ``broken_at`` is the ``event_id`` of the first
    failing event (None when the chain is intact); ``reason`` is a
    human string explaining the failure."""
    project = _resolve_project(conn, principal, project_slug)
    _assert_run_belongs_to_project(conn, run_id, project["project_id"])
    # Round-5 ultra-review (Correctness H): the events page cap is
    # silent for the LIST endpoint (operator iterates and stops
    # when they've seen enough), but verify is a CORRECTNESS gate
    # — silently truncating the chain at the cap and reporting
    # ``ok=True`` for the visible prefix would mislead the
    # operator into believing the FULL chain is intact when only
    # the first ``_EVENTS_MAX`` events were checked.  Refuse with
    # 413 + a clear message until cursor pagination ships.
    total = count_events_for_run(conn, run_id)
    if total > _EVENTS_MAX:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Run has {total} events, exceeding the verify-page "
                f"cap of {_EVENTS_MAX}.  Cursor pagination for "
                f"long-chain verify is on the roadmap; refusing "
                f"rather than reporting a misleading partial-chain "
                f"``ok=True``."
            ),
        )
    events, corrupt = list_events_for_run(conn, run_id, limit=_EVENTS_MAX)
    if corrupt > 0:
        # Don't pretend a chain with corrupt rows verified — the
        # gap is real even if the prefix walks cleanly.  Surface
        # via ``ok=False`` + a structured reason.
        return AuditVerifyResponse(
            ok=False,
            events=len(events),
            broken_at=None,
            reason=(
                f"chain has {corrupt} corrupt event_json row(s); "
                f"cannot verify integrity"
            ),
        )
    result = verify_chain_events(events)
    return AuditVerifyResponse(**result)
