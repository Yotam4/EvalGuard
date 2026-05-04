"""Bearer-token auth: every authenticated endpoint enforces the
configured key, with constant-time compare and a sane WWW-Authenticate
header on 401."""

from __future__ import annotations


def _ingest_payload() -> dict:
    return {
        "schema_version": "1.0.0",
        "run_id": "run_authtest00000",
        "project": "p",
        "trials": [],
    }


def test_missing_authorization_returns_401(client):
    r = client.post("/v1/runs", json=_ingest_payload())
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("WWW-Authenticate", "")


def test_wrong_scheme_returns_401(client):
    r = client.post(
        "/v1/runs", json=_ingest_payload(),
        headers={"Authorization": "Basic dGVzdDp0ZXN0"},
    )
    assert r.status_code == 401


def test_invalid_token_returns_401(client):
    r = client.post(
        "/v1/runs", json=_ingest_payload(),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_valid_token_succeeds(client, auth_headers):
    r = client.post("/v1/runs", json=_ingest_payload(), headers=auth_headers)
    assert r.status_code == 201, r.text


def test_open_mode_skips_auth_check(open_client):
    """Without an API key configured, the server must accept
    unauthenticated requests (with a startup warning the operator
    is expected to heed)."""
    r = open_client.post("/v1/runs", json=_ingest_payload())
    assert r.status_code == 201, r.text


def test_get_run_also_enforces_auth(client, auth_headers):
    """All authenticated endpoints, not just the write side."""
    client.post("/v1/runs", json=_ingest_payload(), headers=auth_headers)
    unauth = client.get("/v1/runs/run_authtest00000")
    assert unauth.status_code == 401
    auth = client.get("/v1/runs/run_authtest00000", headers=auth_headers)
    assert auth.status_code == 200
