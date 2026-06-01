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


# ---------------------------------------------------------------------------
# /v1/ready — deep readiness probe (round-4 big-ticket #2)


def test_ready_returns_200_with_all_checks_green(client):
    """Default test client → fresh DB → alembic at head → evaluators
    loaded.  All three checks must be ok and the overall flag too."""
    r = client.get("/v1/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["checks"]["db"]["ok"] is True
    assert body["checks"]["migration"]["ok"] is True
    assert body["checks"]["evaluators"]["ok"] is True
    assert body["checks"]["evaluators"]["evaluators"] > 0
    assert body["checks"]["evaluators"]["providers"]  > 0


def test_ready_does_not_require_auth(client):
    """Readiness probes come from infra without credentials — same
    no-auth posture as /v1/health."""
    # Use a raw fetch with no Authorization header.
    r = client.get("/v1/ready", headers={})
    assert r.status_code == 200


def test_ready_503_when_migration_drift(client, monkeypatch):
    """Simulate "code says head is X but DB is at Y" by patching the
    script-directory head accessor.  Real-world: rolling deploy where
    the new pod starts before the old one's migration completes — the
    LB should pull it out of rotation until the schema catches up."""
    import evalguard_api.routes.health as health_mod
    from alembic.script import ScriptDirectory

    real_get_current_head = ScriptDirectory.get_current_head

    def _fake_head(self):
        return "9999_phantom_head"

    monkeypatch.setattr(ScriptDirectory, "get_current_head", _fake_head)
    try:
        r = client.get("/v1/ready")
    finally:
        monkeypatch.setattr(ScriptDirectory, "get_current_head", real_get_current_head)
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["migration"]["ok"] is False
    assert "alembic version drift" in body["checks"]["migration"]["error"]


def test_ready_503_when_db_unreachable(client, monkeypatch):
    """If the engine refuses every connect, readiness fails closed
    (with a 503) instead of pretending the server can take traffic."""
    from sqlalchemy.exc import OperationalError

    original_connect = client.app.state.engine.connect

    def _broken_connect(*args, **kwargs):
        raise OperationalError("simulated", {}, BaseException("conn refused"))

    monkeypatch.setattr(client.app.state.engine, "connect", _broken_connect)
    try:
        r = client.get("/v1/ready")
    finally:
        monkeypatch.setattr(client.app.state.engine, "connect", original_connect)
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["db"]["ok"] is False
    # When DB is down, migration check should report unreachable
    # rather than running and surfacing a confusing inner exception.
    assert body["checks"]["migration"]["ok"] is False
