"""``/v1/assets`` — cross-run aggregation of the per-run ``assets[]``
rows.

A run carries a list of versioned assets (prompts, datasets, judges,
heuristics, metrics, schemas, rubrics) — one row per loaded asset.
This endpoint groups them by ``(project_id, kind, asset_id)`` so an
operator can ask: "which prompts are in active use? how many
versions of ``summarize_v1`` do we have? which judges appeared in
the last week?"

Scoping mirrors ``/v1/runs``:
- Members see only assets attached to runs in their own org.
- Admins see everything (filterable by ``?project=`` slug).
- Cross-org enumeration is impossible because the inner-join with
  the projects table filters at the DB layer (and Postgres RLS
  pins it again).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.deps import get_conn
from evalguard_api.models import AssetList, AssetSummary

router = APIRouter()


# Whitelist the asset kinds that can be filtered on. The ``assets``
# table happily stores anything; this list is the public surface and
# matches ``$defs.asset.kind`` in evalguard.run.schema.json.
_KNOWN_KINDS: frozenset[str] = frozenset({
    "prompt", "dataset", "schema", "rubric",
    "judge", "heuristic", "metric",
})


@router.get("/v1/assets", response_model=AssetList, tags=["assets"])
def list_assets(
    kind:    str | None = Query(default=None,
                                description="Filter to one asset kind (prompt/dataset/judge/...)."),
    project: str | None = Query(default=None,
                                description="Filter by project slug."),
    limit:   int = Query(default=100, ge=1, le=500),
    conn:    Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> AssetList:
    if kind is not None and kind not in _KNOWN_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown asset kind {kind!r}. Allowed: {sorted(_KNOWN_KINDS)}.",
        )

    # Build the WHERE incrementally with bind params — never f-strings.
    clauses: list[str] = []
    params: dict = {"limit": limit}
    if not principal.is_admin:
        clauses.append(
            "a.project_id IN (SELECT project_id FROM projects WHERE org_id = :org_id)"
        )
        params["org_id"] = principal.org_id
    if kind is not None:
        clauses.append("a.kind = :kind")
        params["kind"] = kind
    if project is not None:
        clauses.append("r.project_name = :project")
        params["project"] = project
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    # Two correlated subqueries pull the most-recent run per
    # ``(kind, asset_id)`` group: ``MAX(ingested_at)`` then a join
    # back to fetch the run_id and version_id for that timestamp.
    # ``COUNT(DISTINCT version_id)`` and ``COUNT(DISTINCT run_id)``
    # do the version + run aggregation in one pass.
    sql = text(f"""
        WITH agg AS (
          SELECT
            a.project_id                  AS project_id,
            a.kind                        AS kind,
            a.asset_id                    AS asset_id,
            COUNT(DISTINCT a.version_id)  AS version_count,
            COUNT(DISTINCT a.run_id)      AS run_count,
            MAX(r.ingested_at)            AS last_seen
          FROM assets a
          JOIN runs r ON r.run_id = a.run_id
          {where}
          GROUP BY a.project_id, a.kind, a.asset_id
        )
        SELECT
          agg.project_id,
          p.name                          AS project_name,
          agg.kind,
          agg.asset_id,
          agg.version_count,
          agg.run_count,
          agg.last_seen,
          (SELECT a2.run_id     FROM assets a2 JOIN runs r2 ON r2.run_id=a2.run_id
            WHERE a2.project_id=agg.project_id AND a2.kind=agg.kind
              AND a2.asset_id=agg.asset_id AND r2.ingested_at=agg.last_seen
            LIMIT 1) AS last_run_id,
          (SELECT a3.version_id FROM assets a3 JOIN runs r3 ON r3.run_id=a3.run_id
            WHERE a3.project_id=agg.project_id AND a3.kind=agg.kind
              AND a3.asset_id=agg.asset_id AND r3.ingested_at=agg.last_seen
            LIMIT 1) AS last_version_id
        FROM agg
        JOIN projects p ON p.project_id = agg.project_id
        ORDER BY agg.last_seen DESC
        LIMIT :limit
    """)
    rows = conn.execute(sql, params).mappings().fetchall()
    return AssetList(assets=[AssetSummary(**dict(r)) for r in rows])
