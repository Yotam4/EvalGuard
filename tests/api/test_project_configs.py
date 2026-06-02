"""``/v1/projects/{slug}/config*`` — Phase PROXY-1 end-to-end coverage.

The POST is the content-addressed UPSERT (re-pushing identical bytes
is idempotent, different bytes mints a new revision).  GET surfaces
the latest revision; ``/history`` is newest-first metadata; the
per-id GET fetches a specific revision verbatim.  Cross-org access
returns 404 (anti-enumeration).
"""

from __future__ import annotations

import hashlib


def _yaml(project: str = "default") -> str:
    """Tiny but realistic ``evalguard.yaml`` body for round-trip tests."""
    return (
        "version: 1\n"
        f"project: {project}\n"
        "providers: [{ id: 'mock:m', config: { mode: echo } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
    )


# ---------------------------------------------------------------------------
# POST happy path + idempotency


def test_post_config_creates_new_revision_201(client, auth_headers):
    body = _yaml()
    r = client.post(
        "/v1/projects/default/config",
        json={"content": body},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["project_id"]
    assert out["content_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert out["content"] == body
    assert out["pushed_by"]
    assert out["pushed_at"]


def test_post_same_content_returns_200_with_same_id(client, auth_headers):
    """Idempotency: re-pushing identical bytes returns the existing
    record with HTTP 200, not a new id."""
    body = _yaml()
    r1 = client.post(
        "/v1/projects/default/config",
        json={"content": body},
        headers=auth_headers,
    )
    assert r1.status_code == 201
    first_id = r1.json()["id"]

    r2 = client.post(
        "/v1/projects/default/config",
        json={"content": body},
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == first_id
    assert r2.json()["pushed_at"] == r1.json()["pushed_at"]


def test_post_changed_content_creates_new_revision(client, auth_headers):
    """Different bytes → new id, distinct SHA, distinct pushed_at."""
    r1 = client.post(
        "/v1/projects/default/config",
        json={"content": _yaml(project="v1")},
        headers=auth_headers,
    )
    r2 = client.post(
        "/v1/projects/default/config",
        json={"content": _yaml(project="v2")},
        headers=auth_headers,
    )
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
    assert r1.json()["content_sha256"] != r2.json()["content_sha256"]


def test_post_rejects_empty_content_422(client, auth_headers):
    r = client.post(
        "/v1/projects/default/config",
        json={"content": ""},
        headers=auth_headers,
    )
    assert r.status_code == 422


def test_post_rejects_extra_fields_422(client, auth_headers):
    """``_Strict`` base — unknown fields surface as 422 so client
    bugs don't go silent."""
    r = client.post(
        "/v1/projects/default/config",
        json={"content": _yaml(), "totally_unexpected": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Round-4 review-pass: push-time proxy-essential shape validation


def test_post_rejects_yaml_without_version(client, auth_headers):
    bad = "project: default\nproviders: [{ id: 'mock:m' }]\n"
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert "version" in r.text.lower()


def test_post_rejects_yaml_without_project(client, auth_headers):
    bad = "version: 1\nproviders: [{ id: 'mock:m' }]\n"
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "project" in r.text.lower()


def test_post_rejects_yaml_with_empty_providers(client, auth_headers):
    bad = "version: 1\nproject: default\nproviders: []\n"
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "providers" in r.text.lower()


def test_post_rejects_yaml_with_provider_missing_id(client, auth_headers):
    bad = (
        "version: 1\n"
        "project: default\n"
        "providers:\n  - config: { mode: echo }\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "id" in r.text.lower()


def test_post_rejects_malformed_yaml(client, auth_headers):
    """Tab-indented YAML is a classic operator footgun — must 422
    at push, not at first invoke."""
    bad = "version: 1\n\tproject: default\n"   # tab indent
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "valid yaml" in r.text.lower()


def test_post_rejects_provider_config_non_mapping(client, auth_headers):
    """Round-4 ultra-review (Agent-2 G): ``providers[0].config`` must
    be a YAML mapping when set; a scalar slips through to load_provider
    and 500s the invoke call with an opaque AttributeError."""
    bad = (
        "version: 1\n"
        "project: default\n"
        "providers:\n  - id: 'mock:m'\n    config: 'not a mapping'\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert "config" in r.text.lower()


def test_post_rejects_negative_rate_limit(client, auth_headers):
    """Round-4 ultra-review (Agent-3 F): ``rate_limit_per_minute: -1``
    would pass the ``≤ 0 → disabled`` branch in quotas.py and
    silently remove rate limiting.  Must 422 at push."""
    bad = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m' }]\n"
        "rate_limit_per_minute: -1\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "rate_limit_per_minute" in r.text


def test_post_rejects_string_rate_limit(client, auth_headers):
    bad = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m' }]\n"
        "rate_limit_per_minute: 'unlimited'\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422


def test_post_rejects_nan_cost_cap(client, auth_headers):
    """Round-4 ultra-review (Agent-3 F): ``NaN > 0`` is False so the
    cap silently bypasses.  Must 422 at push."""
    bad = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m' }]\n"
        "cost_cap_usd_daily: .nan\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "cost_cap_usd_daily" in r.text


def test_post_rejects_negative_cost_cap(client, auth_headers):
    bad = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m' }]\n"
        "cost_cap_usd_daily: -5.0\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert r.status_code == 422


def test_post_warns_on_cost_cap_usd_typo(client, auth_headers, caplog):
    """Round-4 ultra-review (Agent-3 K): operator wrote
    ``cost_cap_usd`` (CLI executor field) instead of
    ``cost_cap_usd_daily`` (proxy field).  Push succeeds (the field
    is legal for batch) but emits a structured WARNING so the
    confusion surfaces in the access log."""
    import logging
    yaml_blob = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m' }]\n"
        "cost_cap_usd: 1.00\n"
    )
    caplog.set_level(logging.WARNING, logger="evalguard.api.configs")
    r = client.post(
        "/v1/projects/default/config",
        json={"content": yaml_blob},
        headers=auth_headers,
    )
    assert r.status_code in (200, 201)
    warns = [rec.getMessage() for rec in caplog.records
             if rec.levelname == "WARNING"]
    assert any("config.typo_warn" in line for line in warns), warns


def test_post_accepts_proxy_config_without_datasets(client, auth_headers):
    """A pure-proxy config has no datasets — the lighter validation
    (vs the full evalguard.schema.json) must accept it."""
    proxy_only = (
        "version: 1\n"
        "project: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo } }]\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": proxy_only},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# GET latest


def test_get_latest_404_when_no_config_pushed(client, auth_headers):
    r = client.get("/v1/projects/default/config", headers=auth_headers)
    assert r.status_code == 404


def test_get_latest_returns_most_recent_push(client, auth_headers):
    client.post(
        "/v1/projects/default/config",
        json={"content": _yaml(project="old")},
        headers=auth_headers,
    )
    r2 = client.post(
        "/v1/projects/default/config",
        json={"content": _yaml(project="new")},
        headers=auth_headers,
    )
    latest = client.get("/v1/projects/default/config", headers=auth_headers)
    assert latest.status_code == 200
    assert latest.json()["id"] == r2.json()["id"]
    assert "project: new" in latest.json()["content"]


# ---------------------------------------------------------------------------
# History


def test_history_lists_newest_first(client, auth_headers):
    ids = []
    for proj in ("a", "b", "c"):
        r = client.post(
            "/v1/projects/default/config",
            json={"content": _yaml(project=proj)},
            headers=auth_headers,
        )
        ids.append(r.json()["id"])
    h = client.get(
        "/v1/projects/default/config/history?limit=10",
        headers=auth_headers,
    )
    assert h.status_code == 200
    listed = [c["id"] for c in h.json()["configs"]]
    # Newest first.
    assert listed == list(reversed(ids))
    # ``content`` is omitted from history entries (summary shape).
    for entry in h.json()["configs"]:
        assert "content" not in entry


def test_history_respects_limit(client, auth_headers):
    for proj in ("a", "b", "c"):
        client.post(
            "/v1/projects/default/config",
            json={"content": _yaml(project=proj)},
            headers=auth_headers,
        )
    h = client.get(
        "/v1/projects/default/config/history?limit=2",
        headers=auth_headers,
    )
    assert len(h.json()["configs"]) == 2


# ---------------------------------------------------------------------------
# Per-id GET


def test_get_by_id_returns_exact_revision(client, auth_headers):
    r1 = client.post(
        "/v1/projects/default/config",
        json={"content": _yaml(project="v1")},
        headers=auth_headers,
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": _yaml(project="v2")},
        headers=auth_headers,
    )
    # Fetching the OLDER id returns the original bytes, not the
    # latest (the per-id endpoint is the pin-to-revision surface
    # the proxy will use).
    g = client.get(
        f"/v1/projects/default/config/{r1.json()['id']}",
        headers=auth_headers,
    )
    assert g.status_code == 200
    assert g.json()["id"] == r1.json()["id"]
    assert "project: v1" in g.json()["content"]


def test_get_by_id_wrong_project_returns_404(client, auth_headers, make_org, make_member_token):
    """Passing another project's id under this slug must 404 —
    same anti-enumeration shape as the rest of the API.  Use a
    second project (in the default org) to keep the test
    SQLite-only — RLS isn't on this backend, so the test exercises
    the route's explicit ``WHERE id = :id AND project_id = :pid``
    join, not RLS."""
    # Create a second project in the default org via run-ingest.
    other = client.post(
        "/v1/projects",
        json={"slug": "other-proj", "name": "Other"},
        headers=auth_headers,
    )
    assert other.status_code == 201

    r = client.post(
        "/v1/projects/other-proj/config",
        json={"content": _yaml(project="other")},
        headers=auth_headers,
    )
    other_id = r.json()["id"]

    # Same id but under the wrong slug → 404.
    g = client.get(
        f"/v1/projects/default/config/{other_id}",
        headers=auth_headers,
    )
    assert g.status_code == 404


# ---------------------------------------------------------------------------
# Cross-tenant isolation


def test_member_cannot_push_to_other_orgs_project(
    client, auth_headers, make_org, make_member_token,
):
    """Admin pushes a config to acme; a default-org member who
    knows the slug must 404, never the bytes or even an existence
    confirmation."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    # ACME member uploads a config — provision an ACME project
    # first via the same per-org slug used elsewhere.
    client.post(
        "/v1/projects",
        json={"slug": "secret", "name": "Secret"},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    r = client.post(
        "/v1/projects/secret/config",
        json={"content": _yaml(project="secret")},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    assert r.status_code == 201

    # Default-org member can't see it.
    g = client.get(
        "/v1/projects/secret/config",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert g.status_code == 404
    # Default-org member can't push to it either.
    p = client.post(
        "/v1/projects/secret/config",
        json={"content": _yaml(project="hijack")},
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert p.status_code == 404


def test_unknown_project_slug_returns_404(client, auth_headers):
    g = client.get("/v1/projects/does-not-exist/config", headers=auth_headers)
    assert g.status_code == 404
    p = client.post(
        "/v1/projects/does-not-exist/config",
        json={"content": _yaml()},
        headers=auth_headers,
    )
    assert p.status_code == 404


# ---------------------------------------------------------------------------
# Auth


def test_unauthenticated_returns_401(client):
    r = client.post(
        "/v1/projects/default/config",
        json={"content": _yaml()},
    )
    assert r.status_code == 401
    g = client.get("/v1/projects/default/config")
    assert g.status_code == 401
