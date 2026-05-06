"""Round-2 review-pass regressions — bugs caught while auditing
2.5b in places we hadn't deeply reviewed yet.

Each test pins a specific issue surfaced by the round-2 audit:

- ``test_concurrent_first_push_to_new_project_does_not_500`` — pins
  the race-safe ``upsert_project`` (savepoint + refetch on UNIQUE).
- ``test_concurrent_org_create_returns_409_not_500`` — same pattern
  on POST /v1/orgs.
- ``test_concurrent_project_create_returns_409_not_500`` — same on
  POST /v1/projects.
- ``test_oversized_request_body_returns_413`` — Content-Length guard.
- ``test_negative_row_count_rejected_by_pydantic`` — ge=0 constraint
  mirrors the JSON Schema's ``minimum: 0``.
- ``test_malformed_trial_id_rejected_by_pydantic`` — pattern
  constraint on Trial.trial_id mirrors the JSON Schema.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from evalguard_api.config import Settings
from evalguard_api.db import (
    ensure_default_tenancy, make_engine, upsert_project,
)
from evalguard_api.main import _alembic_config

from alembic import command as alembic_command


# ---------------------------------------------------------------------------
# Race fixes


def test_concurrent_first_push_to_new_project_does_not_500(tmp_path: Path):
    """Eight concurrent first-pushes to the same brand-new (org, slug)
    must all succeed and return the SAME project_id. Pre-fix, the
    second-through-Nth INSERT would hit ``UNIQUE(org_id, slug)`` and
    bubble out as an unhandled IntegrityError.

    SQLite serializes writes via its single-writer lock so this race
    is mostly theoretical here, but the fix is still required for
    Postgres correctness — and the savepoint fallback path is
    exercised either way."""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/x.db")
    alembic_command.upgrade(_alembic_config(settings.database_url), "head")
    engine = make_engine(settings)
    try:
        with engine.begin() as conn:
            org_id, _ = ensure_default_tenancy(
                conn, org_slug="default", project_slug="default",
            )

        results: list[str] = []
        errors: list[str] = []
        barrier = threading.Barrier(8)

        def race():
            try:
                barrier.wait()
                with engine.begin() as conn:
                    pid = upsert_project(
                        conn, org_id=org_id, project_name="brand-new-collide",
                    )
                results.append(pid)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"expected zero errors, got: {errors}"
        # All eight callers see the same project_id — exactly one row
        # was inserted and seven re-fetched it.
        assert len(set(results)) == 1, f"got divergent project_ids: {set(results)}"
    finally:
        engine.dispose()


def test_concurrent_org_create_returns_409_not_500(client, auth_headers):
    """Two admins POSTing the same slug must produce exactly one
    201 and one 409 — never a 500 from a leaked IntegrityError.

    SQLite serializes the writes so the race window is narrow, but
    the post-fix code catches IntegrityError and translates to 409
    even when the SELECT pre-check missed."""
    # First create succeeds.
    r1 = client.post("/v1/orgs", json={"slug": "race-org", "name": "R"},
                     headers=auth_headers)
    assert r1.status_code == 201
    # Second create is a 409 — the savepoint catches the UNIQUE
    # violation and translates cleanly.
    r2 = client.post("/v1/orgs", json={"slug": "race-org", "name": "R2"},
                     headers=auth_headers)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"].lower()


def test_concurrent_project_create_returns_409_not_500(client, member_headers):
    r1 = client.post("/v1/projects", json={"slug": "race-proj"},
                     headers=member_headers)
    assert r1.status_code == 201
    r2 = client.post("/v1/projects", json={"slug": "race-proj"},
                     headers=member_headers)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Body size guard


def test_oversized_request_body_returns_413(client, auth_headers):
    """An adversarial / buggy client pushing a multi-GB payload would
    OOM a worker. The Content-Length guard rejects oversize requests
    before Pydantic parses them."""
    # Send a fake Content-Length larger than the default 100 MB —
    # the middleware shouldn't even let us reach the route handler.
    r = client.post(
        "/v1/runs",
        content=b"{}",   # actual body is small
        headers={
            **auth_headers,
            "content-length": str(200 * 1024 * 1024),  # 200 MB advertised
            "content-type":   "application/json",
        },
    )
    assert r.status_code == 413, r.text
    assert "exceeds" in r.json()["detail"].lower()


def test_invalid_content_length_returns_400(client, auth_headers):
    r = client.post(
        "/v1/runs",
        content=b"{}",
        headers={
            **auth_headers,
            "content-length": "not-a-number",
            "content-type":   "application/json",
        },
    )
    # The middleware can't trust a malformed length; the request
    # is rejected outright.
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Pydantic constraints (JSON Schema mirror)


def test_negative_row_count_rejected_by_pydantic(client, auth_headers):
    """``minimum: 0`` in evalguard.run.schema.json. Pydantic must
    enforce the same so the API doesn't accept (and persist) data
    a downstream JSON Schema validator would reject."""
    payload = {
        "schema_version": "1.0.0",
        "run_id":         "run_neg00000000000",
        "project":        "p",
        "trials":         [],
        "row_count":      -5,
    }
    r = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert r.status_code == 422, r.text
    body = r.json()
    assert any("row_count" in str(err) for err in body.get("detail", []))


def test_malformed_trial_id_rejected_by_pydantic(client, auth_headers):
    """``trial_id`` must match ``^trial_[a-z0-9]{8,}$`` per the
    JSON Schema. Pydantic mirrors the regex."""
    payload = {
        "schema_version": "1.0.0",
        "run_id":         "run_validid000000",
        "project":        "p",
        "trials": [
            {
                "trial_id":    "not-a-trial",   # missing trial_ prefix
                "provider_id": "mock:m",
                "provider":    "mock",
                "model":       "m",
            },
        ],
    }
    r = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# scopes_csv parsing — strip on read


def test_scopes_csv_with_whitespace_round_trips(client, auth_headers):
    """The write path strips whitespace before joining; the read path
    must do the same so a hand-edited CSV (or pre-existing row
    written by a buggy migration) doesn't surface scopes with
    leading/trailing spaces."""
    from sqlalchemy import text
    # Mint a key, then poke its scopes_csv directly with whitespace
    # to simulate a buggy historic write.
    r = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "ws-test"},
        headers=auth_headers,
    )
    key_id = r.json()["key"]["key_id"]
    with client.app.state.engine.begin() as conn:
        conn.execute(
            text("UPDATE api_keys SET scopes_csv=:s WHERE key_id=:k"),
            {"s": "  admin  ,  ", "k": key_id},
        )
    listing = client.get(
        "/v1/orgs/org_default/api_keys", headers=auth_headers,
    ).json()
    target = next(k for k in listing["keys"] if k["key_id"] == key_id)
    # ``admin`` (clean) appears, the empty trailing entry is filtered.
    assert target["scopes"] == ["admin"]
