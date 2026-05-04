"""``GET /v1/health`` — uptime + mode advertisement."""

from __future__ import annotations


def test_health_returns_ok_with_auth_mode(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "auth"
    assert body["db"] == "sqlite"
    assert body["version"]


def test_health_advertises_open_mode_when_no_key_configured(open_client):
    r = open_client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["mode"] == "open"


def test_health_does_not_require_auth(client):
    """Health checks come from load balancers without credentials —
    the endpoint must be unauthenticated even when API_KEY is set."""
    r = client.get("/v1/health")
    assert r.status_code == 200


def test_openapi_doc_published(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "EvalGuard API"
    assert "/v1/runs" in spec["paths"]
    assert "/v1/runs/{run_id}" in spec["paths"]
