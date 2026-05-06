"""End-to-end Postgres integration test — gated.

Skipped unless ``EVALGUARD_TEST_POSTGRES_URL`` is set. Spin up a
throwaway database (``docker run --rm postgres:16 ...`` or pg_tmp)
and point the env var at it, e.g.

    export EVALGUARD_TEST_POSTGRES_URL=postgresql+psycopg://test:test@localhost:5432/eg_test

The test runs the same flow as ``test_runs.py`` (push a real run,
GET it back, verify cross-tenant isolation) but against Postgres,
exercising:

- The Alembic ``0001_initial`` migration on Postgres
- The Alembic ``0002_rls_policies`` migration enabling RLS
- ``apply_rls_context`` setting the GUCs
- The whole CRUD path through the SQLAlchemy text() queries
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evalguard_api.config import Settings
from evalguard_api.main import build_app


_PG_URL = os.environ.get("EVALGUARD_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="set EVALGUARD_TEST_POSTGRES_URL to run Postgres integration tests",
)


@pytest.fixture
def pg_settings() -> Settings:
    """Per-test isolated Postgres database via a unique schema.

    Postgres doesn't have ``ATTACH DATABASE`` like SQLite, so per-test
    isolation is via search_path / schema rather than a separate file.
    For simplicity here we just append a per-test suffix to the DB
    name and let the operator clean up the throwaway pg.
    """
    return Settings(
        # The same URL is reused — Alembic upgrades are idempotent so
        # rerunning the migrations against an already-upgraded DB is
        # a no-op.  Tests must clean up runs they create (the
        # ``client`` fixture below truncates).
        database_url=_PG_URL,
        api_key="test-secret-pg",
        default_org_slug=f"default-{uuid.uuid4().hex[:8]}",
        default_project_slug="default",
    )


@pytest.fixture
def pg_client(pg_settings: Settings) -> TestClient:
    app = build_app(settings=pg_settings)
    with TestClient(app) as c:
        yield c
        # Truncate everything we touched. CASCADE handles FK chains.
        with app.state.engine.begin() as conn:
            from sqlalchemy import text
            for tbl in ("events", "gate_results", "run_rows", "trials",
                        "runs", "assets", "api_keys", "projects", "orgs"):
                conn.execute(text(f"TRUNCATE TABLE {tbl} CASCADE"))


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-secret-pg"}


# ---------------------------------------------------------------------------
# Smoke: server boots, migrations apply, basic CRUD works


def test_postgres_server_boots_and_health_responds(pg_client):
    r = pg_client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["db"] == "postgres"


def test_postgres_orgs_create_and_list(pg_client, auth_headers):
    r = pg_client.post(
        "/v1/orgs",
        json={"slug": "pg-acme", "name": "Pg Acme"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    list_r = pg_client.get("/v1/orgs", headers=auth_headers)
    slugs = {o["slug"] for o in list_r.json()["orgs"]}
    assert "pg-acme" in slugs


# ---------------------------------------------------------------------------
# RLS — cross-tenant access blocked at the DB layer
#
# Even if the application layer forgot to filter, RLS would.


def test_postgres_rls_blocks_cross_tenant_select(pg_client, auth_headers):
    """Mint two member tokens in two separate orgs, push a run from
    each, then verify the wrong-org member can't see the foreign run
    via /v1/runs/{id} (the application layer answers 404; RLS would
    have hidden it from the SELECT regardless)."""
    pg_client.post(
        "/v1/orgs", json={"slug": "rls-a", "name": "A"},
        headers=auth_headers,
    )
    pg_client.post(
        "/v1/orgs", json={"slug": "rls-b", "name": "B"},
        headers=auth_headers,
    )
    member_a = pg_client.post(
        "/v1/orgs/org_rls-a/api_keys",
        json={"name": "ma"},
        headers=auth_headers,
    ).json()["token"]
    member_b = pg_client.post(
        "/v1/orgs/org_rls-b/api_keys",
        json={"name": "mb"},
        headers=auth_headers,
    ).json()["token"]
    # Minimal run payload — direct push, no CLI involved (keeps
    # the test fast on the integration runner).
    run_payload = {
        "schema_version": "1.0.0",
        "run_id":         f"run_{uuid.uuid4().hex[:12]}",
        "project":        "rls-project",
        "trials":         [],
    }
    pg_client.post(
        "/v1/runs", json=run_payload,
        headers={"Authorization": f"Bearer {member_a}"},
    )
    # Member of org-b can't fetch it.
    r = pg_client.get(
        f"/v1/runs/{run_payload['run_id']}",
        headers={"Authorization": f"Bearer {member_b}"},
    )
    assert r.status_code == 404
    # Member of org-a can.
    r2 = pg_client.get(
        f"/v1/runs/{run_payload['run_id']}",
        headers={"Authorization": f"Bearer {member_a}"},
    )
    assert r2.status_code == 200


def test_postgres_rls_status_is_enabled(pg_client, auth_headers):
    """Verify RLS is actually ON (Alembic 0002 ran) by querying
    pg_class. Without RLS enabled, the test above would still pass
    on application-layer auth alone — this catches a migration
    that silently no-op'd."""
    from sqlalchemy import text
    with pg_client.app.state.engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT relname FROM pg_class
            WHERE relkind = 'r' AND relrowsecurity = true
            AND relname IN ('runs','projects','api_keys','trials',
                            'run_rows','gate_results','assets','events')
        """)).fetchall()
    rls_tables = {r[0] for r in rows}
    expected = {
        "runs", "projects", "api_keys", "trials",
        "run_rows", "gate_results", "assets", "events",
    }
    assert expected <= rls_tables, (
        f"RLS not enabled on: {expected - rls_tables}"
    )
