"""Bearer-token auth: every authenticated endpoint enforces the
configured key.

NB: the docstring previously claimed "constant-time compare"; the
runtime path is a Postgres-indexed equality lookup on the SHA-256 of
the bearer token, which is constant-ish (no early-return branch on
prefix mismatch) but not a literal ``secrets.compare_digest`` call.
With 128 bits of token entropy and a hashed-at-rest column, that's
acceptable. See ``apps/api/evalguard_api/db.py`` for the lookup; the
``WWW-Authenticate`` header is asserted below.
"""

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


# ---------------------------------------------------------------------------
# Per-org token resolution


def test_freshly_minted_member_token_resolves_to_its_org(client, auth_headers, make_org):
    """Token → api_keys row → Principal with the right org_id.
    A member token's ``org_id`` claim must match the org it was minted
    in — not the caller's org, not the default."""
    make_org("acme")
    create = client.post(
        "/v1/orgs/org_acme/api_keys",
        json={"name": "acme-ci"},
        headers=auth_headers,
    )
    acme_token = create.json()["token"]
    # The token's owner can list keys IN org_acme...
    r = client.get(
        "/v1/orgs/org_acme/api_keys",
        headers={"Authorization": f"Bearer {acme_token}"},
    )
    assert r.status_code == 200
    # ...but NOT in org_default.
    r2 = client.get(
        "/v1/orgs/org_default/api_keys",
        headers={"Authorization": f"Bearer {acme_token}"},
    )
    assert r2.status_code == 403


def test_revoked_token_returns_401(client, auth_headers):
    create = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "doomed"},
        headers=auth_headers,
    )
    token = create.json()["token"]
    key_id = create.json()["key"]["key_id"]

    # Token works while live.
    assert client.get(
        "/v1/orgs", headers={"Authorization": f"Bearer {token}"},
    ).status_code == 200

    # Revoke it.
    client.delete(f"/v1/api_keys/{key_id}", headers=auth_headers)

    # Subsequent calls 401.
    assert client.get(
        "/v1/orgs", headers={"Authorization": f"Bearer {token}"},
    ).status_code == 401


def test_open_mode_principal_has_admin_semantics(open_client):
    """In open mode (no env key), every caller is an admin in the
    default org so cross-org operations / listings work for local
    dev. The mode is loudly advertised by /v1/health."""
    health = open_client.get("/v1/health").json()
    assert health["mode"] == "open"
    # Open-mode caller can create orgs (admin operation).
    r = open_client.post(
        "/v1/orgs", json={"slug": "openmode", "name": "Open Mode"},
    )
    assert r.status_code == 201
