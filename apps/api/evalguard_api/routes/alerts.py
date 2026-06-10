"""``GET /v1/projects/{slug}/alerts`` — fired-alert history.

The alert engine (``evalguard_api.alerts``) writes one row per
transition into ``alerts``; this route is the read-side surface.
v1 returns newest-first with a simple ``limit`` cap; cursor
pagination ships with the web UI work.

Editable rule CRUD ships in a follow-up: the plan is for the
``/alerts`` web page to write back to ``project_configs`` (creating
a new revision under the hood); for now operators push rules via
the existing ``evalguard push-config`` path.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import resolve_project_or_404
from evalguard_api.deps import get_conn
from evalguard_api.models import AlertEvent, AlertList


router = APIRouter()

_LIST_MAX = 500


@router.get(
    "/v1/projects/{project_slug}/alerts",
    response_model=AlertList,
    tags=["alerts"],
)
def list_project_alerts(
    project_slug: str,
    limit: int = Query(default=50, ge=1, le=_LIST_MAX),
    rule_id: str | None = Query(default=None),
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> AlertList:
    """Return fired-alert rows for one project, newest-first.

    ``rule_id`` narrows to a single rule (handy when the operator
    is investigating one alert's history).  ``limit`` caps the page
    at 500 — the v1 view tolerates a smaller page and cursor
    pagination ships with the editable web UI.
    """
    project = resolve_project_or_404(conn, principal, project_slug)
    project_id = project["project_id"]

    if rule_id is None:
        rows = conn.execute(
            text("""SELECT id, project_id, rule_id, fired_at,
                           window_start, window_end, gate,
                           observed_value, threshold_json,
                           transition, suppressed, notify_results_json
                    FROM alerts
                    WHERE project_id = :pid
                    ORDER BY fired_at DESC, id DESC
                    LIMIT :lim"""),
            {"pid": project_id, "lim": limit},
        ).mappings().fetchall()
    else:
        rows = conn.execute(
            text("""SELECT id, project_id, rule_id, fired_at,
                           window_start, window_end, gate,
                           observed_value, threshold_json,
                           transition, suppressed, notify_results_json
                    FROM alerts
                    WHERE project_id = :pid AND rule_id = :rid
                    ORDER BY fired_at DESC, id DESC
                    LIMIT :lim"""),
            {"pid": project_id, "rid": rule_id, "lim": limit},
        ).mappings().fetchall()

    return AlertList(
        alerts=[_row_to_event(r) for r in rows],
    )


def _row_to_event(r: dict[str, Any]) -> AlertEvent:
    return AlertEvent(
        id=int(r["id"]),
        project_id=r["project_id"],
        rule_id=r["rule_id"],
        fired_at=r["fired_at"],
        window_start=r["window_start"],
        window_end=r["window_end"],
        gate=r["gate"],
        observed_value=_safe_float(r["observed_value"]),
        threshold=_safe_json(r["threshold_json"]) or {},
        transition=r["transition"],
        suppressed=bool(r["suppressed"]),
        notify_results=_safe_json(r["notify_results_json"]) or [],
    )


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_json(v: Any) -> Any:
    if v is None:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None
