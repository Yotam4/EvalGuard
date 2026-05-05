"""``/v1/projects`` — project CRUD scoped to caller's org."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# create


def test_member_can_create_project_in_own_org(client, member_headers):
    r = client.post(
        "/v1/projects",
        json={"slug": "demo", "name": "Demo project"},
        headers=member_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "demo"
    assert body["org_id"] == "org_default"


def test_admin_can_target_other_org_explicitly(client, auth_headers, make_org):
    make_org("acme")
    r = client.post(
        "/v1/projects?org_id=org_acme",
        json={"slug": "rocket"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    assert r.json()["org_id"] == "org_acme"


def test_member_cannot_target_foreign_org(client, make_org, make_member_token):
    make_org("foreign")
    member = make_member_token("org_default")
    r = client.post(
        "/v1/projects?org_id=org_foreign",
        json={"slug": "sneaky"},
        headers={"Authorization": f"Bearer {member}"},
    )
    assert r.status_code == 403


def test_duplicate_slug_in_same_org_returns_409(client, member_headers):
    client.post("/v1/projects", json={"slug": "demo"}, headers=member_headers)
    r = client.post("/v1/projects", json={"slug": "demo"}, headers=member_headers)
    assert r.status_code == 409


def test_same_slug_allowed_in_different_orgs(client, auth_headers, make_org):
    """Two orgs may both have a project named 'demo'; the (org, slug)
    composite is the uniqueness boundary, not slug alone."""
    make_org("acme")
    r1 = client.post(
        "/v1/projects?org_id=org_default",
        json={"slug": "demo"},
        headers=auth_headers,
    )
    r2 = client.post(
        "/v1/projects?org_id=org_acme",
        json={"slug": "demo"},
        headers=auth_headers,
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["project_id"] != r2.json()["project_id"]


# ---------------------------------------------------------------------------
# list


def test_member_lists_only_own_org_projects(client, auth_headers, make_org, member_headers):
    """Listing as a member must NOT include any foreign-org project,
    no matter how many exist there. The default org has its own
    auto-provisioned ``default`` project from the lifespan; we just
    assert the foreign ones don't leak in."""
    make_org("acme")
    client.post("/v1/projects", json={"slug": "p1"}, headers=member_headers)
    client.post(
        "/v1/projects?org_id=org_acme",
        json={"slug": "p2"},
        headers=auth_headers,
    )
    r = client.get("/v1/projects", headers=member_headers)
    assert r.status_code == 200
    slugs = {p["slug"] for p in r.json()["projects"]}
    # Member sees default-org projects only ('default' from lifespan
    # plus 'p1'); 'p2' from the foreign org is silently filtered out.
    assert "p1" in slugs
    assert "p2" not in slugs
    # Every visible project belongs to the member's org.
    for p in r.json()["projects"]:
        assert p["org_id"] == "org_default"


def test_admin_lists_with_explicit_org_filter(client, auth_headers, make_org):
    make_org("acme")
    client.post(
        "/v1/projects?org_id=org_acme",
        json={"slug": "rocket"},
        headers=auth_headers,
    )
    r = client.get("/v1/projects?org_id=org_acme", headers=auth_headers)
    assert r.status_code == 200
    assert {p["slug"] for p in r.json()["projects"]} == {"rocket"}


# ---------------------------------------------------------------------------
# detail


def test_get_project_in_own_org(client, member_headers):
    client.post("/v1/projects", json={"slug": "demo"}, headers=member_headers)
    r = client.get("/v1/projects/demo", headers=member_headers)
    assert r.status_code == 200
    assert r.json()["slug"] == "demo"


def test_get_unknown_project_returns_404(client, member_headers):
    r = client.get("/v1/projects/nope", headers=member_headers)
    assert r.status_code == 404


def test_member_gets_404_not_403_for_foreign_org_project(
    client, auth_headers, make_org, member_headers,
):
    """No info leak: a member querying a foreign-org project sees 404
    (not 403) — same as if the slug doesn't exist."""
    make_org("acme")
    client.post(
        "/v1/projects?org_id=org_acme",
        json={"slug": "secret"},
        headers=auth_headers,
    )
    r = client.get("/v1/projects/secret", headers=member_headers)
    # Member's GET resolves against their own org, where 'secret'
    # doesn't exist → 404. They never reach the foreign org's row.
    assert r.status_code == 404


def test_member_explicitly_targeting_foreign_org_is_403(
    client, member_headers, make_org, auth_headers,
):
    """Even with explicit ``?org_id=`` a non-admin can't escape their
    own org — that path is gated by ``_resolve_target_org``."""
    make_org("acme")
    r = client.get("/v1/projects/anything?org_id=org_acme", headers=member_headers)
    assert r.status_code == 403
