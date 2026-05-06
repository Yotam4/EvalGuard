"""``/v1/orgs`` — org CRUD + cross-tenant isolation pins."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# create


def test_admin_can_create_org(client, auth_headers):
    r = client.post(
        "/v1/orgs",
        json={"slug": "acme", "name": "Acme Inc."},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["org_id"] == "org_acme"
    assert body["slug"] == "acme"
    assert body["name"] == "Acme Inc."
    assert body["created_at"]


def test_member_cannot_create_org(client, member_headers):
    r = client.post(
        "/v1/orgs",
        json={"slug": "shadow", "name": "Shadow Org"},
        headers=member_headers,
    )
    assert r.status_code == 403


def test_duplicate_slug_returns_409(client, auth_headers, make_org):
    make_org("acme")
    r = client.post(
        "/v1/orgs",
        json={"slug": "acme", "name": "Acme 2"},
        headers=auth_headers,
    )
    assert r.status_code == 409


def test_invalid_slug_rejected(client, auth_headers):
    """Slugs must be URL-safe — uppercase / spaces / underscores
    rejected at the Pydantic layer."""
    for bad in ["UPPERCASE", "with space", "ünïcødé", ""]:
        r = client.post(
            "/v1/orgs",
            json={"slug": bad, "name": "x"},
            headers=auth_headers,
        )
        assert r.status_code == 422, f"slug {bad!r} should have been rejected"


# ---------------------------------------------------------------------------
# list


def test_admin_lists_all_orgs(client, auth_headers, make_org):
    make_org("alpha")
    make_org("beta")
    r = client.get("/v1/orgs", headers=auth_headers)
    assert r.status_code == 200
    slugs = {o["slug"] for o in r.json()["orgs"]}
    # Default org provisioned at startup + the two we made.
    assert {"default", "alpha", "beta"} <= slugs


def test_member_sees_only_their_own_org(client, auth_headers, make_org, member_headers):
    """Listing /v1/orgs as a member must NOT leak the existence of
    other orgs — silent scoping rather than 403 per row."""
    make_org("alpha")
    make_org("beta")
    r = client.get("/v1/orgs", headers=member_headers)
    assert r.status_code == 200
    orgs = r.json()["orgs"]
    assert len(orgs) == 1
    assert orgs[0]["slug"] == "default"


# ---------------------------------------------------------------------------
# detail


def test_get_own_org(client, auth_headers):
    r = client.get("/v1/orgs/org_default", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["slug"] == "default"


def test_member_gets_404_not_403_for_foreign_org(client, member_headers, make_org):
    """Cross-tenant info-leak guard: a non-member must not be able
    to confirm whether a foreign org exists. The endpoint returns
    404 for both "doesn't exist" and "you can't see it" — same as
    GitHub's /orgs/{name} behaviour and matches our cross-org
    response on ``GET /v1/runs/{run_id}``."""
    make_org("acme")
    r = client.get("/v1/orgs/org_acme", headers=member_headers)
    assert r.status_code == 404
    # Same status for a slug that genuinely doesn't exist.
    r2 = client.get("/v1/orgs/org_doesnotexist", headers=member_headers)
    assert r2.status_code == 404


def test_admin_can_see_any_org(client, auth_headers, make_org):
    make_org("acme")
    r = client.get("/v1/orgs/org_acme", headers=auth_headers)
    assert r.status_code == 200


def test_unknown_org_returns_404(client, auth_headers):
    r = client.get("/v1/orgs/org_nope", headers=auth_headers)
    assert r.status_code == 404
