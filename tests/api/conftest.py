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
    return {"Authorization": "Bearer test-secret"}
