"""``GET /v1/health`` — uptime + mode advertisement."""

from __future__ import annotations


def test_health_returns_ok_with_auth_mode(client):
    # Round-7 review-pass: anchor ``version`` against the actual
    # package metadata, not just "truthy".  A regression that hard-
    # coded the version (e.g. ``"unknown"``) or dropped it to a
    # placeholder would slip past ``assert body["version"]`` but
    # surface here.
    from evalguard_api import __version__

    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "auth"
    assert body["db"] == "sqlite"
    assert body["version"] == __version__


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
    LB should pull it out of rotation until the schema catches up.

    Test-quality round (Finding 5): the previous version wrapped the
    monkeypatch in ``try/finally`` to restore the real head, but
    ``monkeypatch`` already undoes its own changes at fixture
    teardown — the manual restore was redundant noise.
    """
    from alembic.script import ScriptDirectory

    monkeypatch.setattr(
        ScriptDirectory, "get_current_head",
        lambda self: "9999_phantom_head",
    )
    r = client.get("/v1/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    # Anchor that the DB check passed and only the migration check
    # failed — otherwise the 503 could be triggered by an unrelated
    # failure and the assertion above would be misleading.
    assert body["checks"]["db"]["ok"] is True, body["checks"]
    assert body["checks"]["migration"]["ok"] is False
    assert "alembic version drift" in body["checks"]["migration"]["error"]


def test_ready_503_when_engine_not_yet_attached(client, monkeypatch):
    """Round-4 ultra-review (Agent-1 H): if a k8s probe hits
    /v1/ready before lifespan attaches ``state.engine``, the
    handler must return a clean 503 with the structured body
    (not 500 with an AttributeError trace)."""
    # Temporarily delete the engine attribute to simulate the
    # pre-lifespan race window.
    engine = client.app.state.engine
    delattr(client.app.state, "engine")
    try:
        r = client.get("/v1/ready")
    finally:
        client.app.state.engine = engine
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["db"]["ok"] is False
    assert "engine not initialised" in body["checks"]["db"]["error"]


def test_ready_503_when_evaluator_registry_broken(client, monkeypatch):
    """Round-4 ultra-review (Agent-2 F): a corrupt evaluator-plugin
    install would silently 422 every /invoke call.  /v1/ready must
    flip to 503 so the LB pulls the pod out of rotation."""
    import evalguard_evaluators.registry as registry

    def _broken():
        raise ImportError("simulated entry-point load failure")

    monkeypatch.setattr(registry, "iter_evaluators", _broken)
    r = client.get("/v1/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["evaluators"]["ok"] is False
    assert "ImportError" in body["checks"]["evaluators"]["error"]


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
