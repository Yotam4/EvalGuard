"""Shared fixtures for the API test suite.

Each test gets a freshly initialized SQLite file under ``tmp_path``
so tests can't see each other's runs. The ``client`` fixture wires
the FastAPI ``TestClient`` against that database with an explicit
``Settings`` (bypassing process env).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evalguard_api.config import Settings
from evalguard_api.main import build_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Per-test Settings with an isolated sqlite path."""
    return Settings(
        database_url=f"sqlite:///{tmp_path}/server.db",
        api_key="test-secret",
        default_org_slug="default",
        default_project_slug="default",
        cors_origins=("*",),
        bind_host="127.0.0.1",
        bind_port=0,
    )


@pytest.fixture
def open_settings(tmp_path: Path) -> Settings:
    """Settings with no API key — open mode (dev-only)."""
    return Settings(
        database_url=f"sqlite:///{tmp_path}/server.db",
        api_key="",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    """TestClient wired to a per-test app instance.

    Using ``with`` triggers FastAPI's lifespan, which initializes
    the schema and provisions the default org/project."""
    app = build_app(settings=settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def open_client(open_settings: Settings) -> TestClient:
    app = build_app(settings=open_settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Headers for the bootstrap admin token (materialized at startup
    from ``EVALGUARD_API_KEY``). Use this for tests that need to act
    as a cross-org admin — creating orgs, listing every org, etc."""
    return {"Authorization": "Bearer test-secret"}


# ---------------------------------------------------------------------------
# Multi-tenant fixtures
#
# The ``client`` fixture above already authenticates as the bootstrap
# admin. These helpers let tests carve out additional orgs and per-org
# member tokens so cross-tenant isolation can actually be exercised.


@pytest.fixture
def make_org(client, auth_headers):
    """Factory: create an org and return its row dict.

    Usage:
        org = make_org(slug="acme", name="Acme")
    """
    def _make(slug: str, name: str | None = None) -> dict:
        r = client.post(
            "/v1/orgs",
            json={"slug": slug, "name": name or slug.title()},
            headers=auth_headers,
        )
        assert r.status_code == 201, r.text
        return r.json()
    return _make


@pytest.fixture
def make_member_token(client, auth_headers):
    """Factory: mint a member-scope (no admin) api key for ``org_id``
    and return the plaintext bearer token."""
    def _make(org_id: str, name: str = "test-member") -> str:
        r = client.post(
            f"/v1/orgs/{org_id}/api_keys",
            json={"name": name, "scopes": []},
            headers=auth_headers,
        )
        assert r.status_code == 201, r.text
        return r.json()["token"]
    return _make


@pytest.fixture
def member_headers(make_member_token):
    """Default member token bound to the default org. Convenience for
    tests that just need "an authenticated non-admin"."""
    token = make_member_token("org_default", name="default-member")
    return {"Authorization": f"Bearer {token}"}
