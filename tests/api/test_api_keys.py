"""``/v1/orgs/{org_id}/api_keys`` and ``/v1/api_keys/{key_id}`` —
API-key lifecycle + revocation."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# create


def test_member_can_create_org_scoped_key(client, member_headers):
    r = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "ci"},
        headers=member_headers,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["token"].startswith("evk_"), "token must carry the searchable prefix"
    assert body["key"]["org_id"] == "org_default"
    assert body["key"]["scopes"] == []
    assert body["key"]["prefix"]
    assert "evk_" in body["key"]["prefix"]


def test_member_cannot_grant_admin_scope(client, member_headers):
    """Privilege escalation guard — only admins can mint admin keys."""
    r = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "stealth", "scopes": ["admin"]},
        headers=member_headers,
    )
    assert r.status_code == 403


def test_admin_can_grant_admin_scope(client, auth_headers):
    r = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "ci-admin", "scopes": ["admin"]},
        headers=auth_headers,
    )
    assert r.status_code == 201
    assert "admin" in r.json()["key"]["scopes"]


def test_member_cannot_create_key_in_foreign_org(client, make_org, member_headers):
    make_org("acme")
    r = client.post(
        "/v1/orgs/org_acme/api_keys",
        json={"name": "spy"},
        headers=member_headers,
    )
    assert r.status_code == 403


def test_admin_can_create_key_in_any_org(client, auth_headers, make_org):
    make_org("acme")
    r = client.post(
        "/v1/orgs/org_acme/api_keys",
        json={"name": "audit"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    assert r.json()["key"]["org_id"] == "org_acme"


# ---------------------------------------------------------------------------
# token usage


def test_freshly_minted_token_authenticates(client, auth_headers):
    """The plaintext token returned at creation must immediately work
    on subsequent requests — covers the hash-then-lookup path."""
    r = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "round-trip"},
        headers=auth_headers,
    )
    token = r.json()["token"]
    # Use the token to list its own org's keys.
    r2 = client.get(
        "/v1/orgs/org_default/api_keys",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200


def test_token_plaintext_never_reappears(client, auth_headers):
    """Listing keys must not include the plaintext token — it's
    write-only at creation time."""
    create = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "secret-only-once"},
        headers=auth_headers,
    )
    plaintext = create.json()["token"]
    r = client.get("/v1/orgs/org_default/api_keys", headers=auth_headers)
    body = r.json()
    for key in body["keys"]:
        assert "token" not in key
        assert "hashed_key" not in key
    # And — most importantly — the plaintext must not appear anywhere
    # in the listing response body.
    assert plaintext not in r.text
    # Server-minted keys carry the searchable ``evk_`` prefix.  The
    # bootstrap key materialized from the env is the one exception
    # (its plaintext is whatever the operator chose).
    server_minted = [k for k in body["keys"] if k["name"] != "bootstrap (env)"]
    for k in server_minted:
        assert k["prefix"].startswith("evk_")


# ---------------------------------------------------------------------------
# list


def test_list_returns_all_org_keys(client, auth_headers):
    for n in ("a", "b", "c"):
        client.post(
            "/v1/orgs/org_default/api_keys",
            json={"name": n},
            headers=auth_headers,
        )
    r = client.get("/v1/orgs/org_default/api_keys", headers=auth_headers)
    assert r.status_code == 200
    names = {k["name"] for k in r.json()["keys"]}
    # Bootstrap key + the three we made.
    assert {"a", "b", "c", "bootstrap (env)"} <= names


def test_member_cannot_list_foreign_org_keys(client, make_org, member_headers):
    make_org("acme")
    r = client.get("/v1/orgs/org_acme/api_keys", headers=member_headers)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# revoke


def test_revoke_existing_key(client, auth_headers):
    r = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "revocable"},
        headers=auth_headers,
    )
    key_id = r.json()["key"]["key_id"]
    token = r.json()["token"]

    rev = client.delete(f"/v1/api_keys/{key_id}", headers=auth_headers)
    assert rev.status_code == 204

    # Revoked tokens are 401, not 200.
    r2 = client.get("/v1/orgs", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 401


def test_revoke_is_idempotent(client, auth_headers):
    r = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "x"},
        headers=auth_headers,
    )
    key_id = r.json()["key"]["key_id"]
    assert client.delete(f"/v1/api_keys/{key_id}", headers=auth_headers).status_code == 204
    # Second call still 204 (no error).
    assert client.delete(f"/v1/api_keys/{key_id}", headers=auth_headers).status_code == 204


def test_revoke_unknown_returns_404(client, auth_headers):
    r = client.delete("/v1/api_keys/key_doesnotexist", headers=auth_headers)
    assert r.status_code == 404


def test_member_cannot_revoke_foreign_key(client, auth_headers, make_org, make_member_token):
    """Revoke is gated by org membership — same as create / list."""
    make_org("acme")
    # Mint a key in the foreign org.
    r = client.post(
        "/v1/orgs/org_acme/api_keys",
        json={"name": "victim"},
        headers=auth_headers,
    )
    target_key_id = r.json()["key"]["key_id"]
    # Member of the default org tries to revoke it.
    member = make_member_token("org_default")
    bad = client.delete(
        f"/v1/api_keys/{target_key_id}",
        headers={"Authorization": f"Bearer {member}"},
    )
    assert bad.status_code == 403
