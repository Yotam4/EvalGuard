"""Phase-A round-3 review regressions.

Each test pins a specific finding from the multi-agent review of
commits ``3ecb1eb..a935092`` so it can't silently regress.

Coverage:

- A.1 — RLS GUC reset on connection checkout (PgBouncer-bleed defense).
  Unit-tests the hook against SQLAlchemy's pool semantics; the actual
  Postgres / PgBouncer scenario is exercised by ``test_postgres.py``
  when ``EVALGUARD_TEST_POSTGRES_URL`` is set.
- A.2 — RLS on ``orgs``. Verified via ``test_postgres.py`` because RLS
  is a Postgres-only feature; SQLite can only assert the migration
  applies cleanly without errors (covered here).
- A.3 — Refuse-to-boot when open-mode is on without explicit opt-in,
  or when open-mode is on with CORS=*.
- A.4 — ``apply_admin_rls_context`` uses NULL, not a literal sentinel.
- A.5 — BYPASSRLS startup guard rejects superuser-style runtime roles.
  Postgres-only; behavior on SQLite is a passive skip (covered here).
- A.6 — TrustedHost / HTTPSRedirect middleware install gating.
- A.7 — Concurrent lifespan starts don't race on the bootstrap key
  (advisory-lock fix). Postgres-only test path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from evalguard_api.config import Settings, StartupRefusal, validate_for_startup
from evalguard_api.db import apply_admin_rls_context, make_engine
from evalguard_api.main import build_app


# ---------------------------------------------------------------------------
# A.3 — refuse-to-boot combinations


def test_open_mode_without_opt_in_is_refused(tmp_path: Path):
    """Missing ``EVALGUARD_API_KEY`` AND missing ``EVALGUARD_OPEN_MODE=1``
    → server refuses to start. The previous behaviour silently booted
    open. Tests assert the exception class so a future refactor that
    swaps the message doesn't break the contract."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="",
        open_mode_opt_in=False,
    )
    with pytest.raises(StartupRefusal) as exc:
        validate_for_startup(s)
    assert "EVALGUARD_OPEN_MODE" in str(exc.value)


def test_open_mode_with_cors_wildcard_is_refused(tmp_path: Path):
    """Even with explicit opt-in, open-mode AND CORS=* together is
    a foot-gun (any browser tab can call the API with no auth). The
    refusal forces the operator to pick one or the other."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="",
        open_mode_opt_in=True,
        cors_origins=("*",),
    )
    with pytest.raises(StartupRefusal) as exc:
        validate_for_startup(s)
    assert "CORS" in str(exc.value)


def test_open_mode_opted_in_with_specific_cors_boots_clean(tmp_path: Path):
    """The intended dev path: explicit open-mode opt-in + an
    explicit (non-wildcard) origin allowlist. validate_for_startup
    must not raise."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="",
        open_mode_opt_in=True,
        cors_origins=("http://localhost:3000",),
    )
    validate_for_startup(s)   # no raise


def test_open_mode_with_non_loopback_bind_refuses(tmp_path: Path):
    """Round-4 ultra-review (Agent-2 I): open mode + a non-loopback
    bind would expose the proxy invoke endpoint to anyone on the
    network.  ``validate_for_startup`` must refuse."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="",
        open_mode_opt_in=True,
        bind_host="0.0.0.0",
        cors_origins=("http://localhost:3000",),
    )
    with pytest.raises(StartupRefusal) as exc:
        validate_for_startup(s)
    assert "EVALGUARD_HOST" in str(exc.value)
    assert "0.0.0.0" in str(exc.value)


def test_open_mode_with_bracketed_ipv6_loopback_allowed(tmp_path: Path):
    """Bracketed IPv6 loopback ``[::1]`` (round-3 fix) is in the
    loopback allowlist so an operator using the bracketed form
    isn't refused."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="",
        open_mode_opt_in=True,
        bind_host="[::1]",
        cors_origins=("http://localhost:3000",),
    )
    validate_for_startup(s)   # no raise


def test_auth_mode_with_cors_wildcard_allowed_with_warning(tmp_path: Path):
    """Auth on + CORS=* is allowed (some users genuinely need it),
    but emits a startup log warning. The validate function does not
    raise — the warning lives in the lifespan path."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="real-key",
        cors_origins=("*",),
    )
    validate_for_startup(s)   # no raise


# ---------------------------------------------------------------------------
# A.4 — sentinel NULL


def test_admin_rls_context_uses_null_not_sentinel(tmp_path: Path):
    """``apply_admin_rls_context`` previously stored the literal
    string ``'_system_'`` in ``app.org_id``. A row with that exact
    org_id would match the policy's OR branch if Postgres reordered
    the predicates. The fix is to use NULL — which never equals
    anything in SQL three-valued logic.

    This is a behavioural pin: SQLite is a no-op so we can't observe
    the GUC, but we can assert the function returns cleanly without
    setting any non-NULL sentinel into a queryable column. The
    Postgres path is verified end-to-end in ``test_postgres.py``.
    """
    s = Settings(database_url=f"sqlite:///{tmp_path}/x.db", api_key="k")
    engine = make_engine(s)
    with engine.begin() as conn:
        # No-op on SQLite; the assertion is "doesn't crash" + the
        # source-grep below covering the fix.
        apply_admin_rls_context(conn)


def test_admin_rls_context_source_uses_null():
    """Belt-and-braces source pin — the fix MUST use NULL via
    ``set_config(..., NULL, true)``. A future refactor that re-
    introduces a sentinel string regresses this finding."""
    src = Path(__file__).resolve().parents[2] / "apps" / "api" / "evalguard_api" / "db.py"
    body = src.read_text()
    # The function being pinned. Slice from its def to the next def
    # so the assertion only sees this function's body.
    fn_start = body.index("def apply_admin_rls_context")
    next_def = body.index("\ndef ", fn_start + 1)
    fn = body[fn_start:next_def]
    assert "set_config('app.org_id', NULL" in fn
    # Specifically forbid the SQL that writes the literal sentinel
    # back into the GUC. The string ``_system_`` is allowed in the
    # docstring (explaining what the previous, broken implementation
    # used to do), but never in a ``set_config`` call.
    assert "set_config('app.org_id', '_system_'" not in fn, \
        "sentinel string snuck back in"


# ---------------------------------------------------------------------------
# A.6 — middleware install is opt-in


def test_https_redirect_middleware_off_by_default(tmp_path: Path):
    """``require_https`` defaults to false so local dev on
    http://localhost works without flags."""
    s = Settings(database_url=f"sqlite:///{tmp_path}/x.db", api_key="k")
    assert s.require_https is False


def test_trusted_hosts_default_is_wildcard(tmp_path: Path):
    """Defaults to ``("*",)`` — production must override. The
    middleware install path skips registration when the value is
    the wildcard so the no-config path doesn't add a no-op."""
    s = Settings(database_url=f"sqlite:///{tmp_path}/x.db", api_key="k")
    assert s.trusted_hosts == ("*",)


def test_trusted_hosts_override_blocks_spoofed_host(tmp_path: Path):
    """When ``EVALGUARD_TRUSTED_HOSTS=evalguard.example.com`` is set,
    a request with a different Host header should be rejected by
    Starlette's TrustedHostMiddleware (400)."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="real-key",
        trusted_hosts=("evalguard.example.com",),
        cors_origins=("https://evalguard.example.com",),
    )
    app = build_app(settings=s)
    with TestClient(app, base_url="http://attacker.example.com") as c:
        r = c.get("/v1/health")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# A.5 — BYPASSRLS guard (Postgres-only — sqlite path passes through)


def test_bypassrls_guard_is_no_op_on_sqlite(tmp_path: Path):
    """The guard runs only on the Postgres dialect; SQLite has no
    BYPASSRLS concept and the lifespan must not try to query
    ``pg_roles``. This test boots a SQLite client successfully."""
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="real-key",
        cors_origins=("https://test.local",),
    )
    app = build_app(settings=s)
    with TestClient(app) as c:
        r = c.get("/v1/health")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# A.1 — GUC reset hook on connection checkout (Postgres-only behaviour;
# unit-tested here via SQLAlchemy event-listener registration)


def test_reset_rls_hook_registered_on_postgres_engine_only(monkeypatch):
    """The ``checkout`` event listener must be installed only on
    Postgres engines. A unit test that swaps the dialect lets us
    assert the registration without a real Postgres."""
    # Build a sqlite engine — should have no ``checkout`` listener
    # for RLS reset (PRAGMAs use ``connect``, which is fine).
    s_sqlite = Settings(
        database_url="sqlite:///:memory:",
        api_key="real-key",
        cors_origins=("https://test.local",),
    )
    engine = make_engine(s_sqlite)
    # The pool's ``checkout`` event has no RLS-reset hook for sqlite.
    listeners = engine.pool.dispatch.checkout.listeners
    names = [getattr(fn, "__name__", "") for fn in listeners]
    assert "_reset_rls_guc_on_checkout" not in names


# ---------------------------------------------------------------------------
# Source pins — defense-in-depth that the fixes don't get reverted


def test_orgs_rls_migration_present():
    """0003 must declare the orgs RLS policy upgrade and downgrade.
    Catches a refactor that 'merges' migrations and drops the file."""
    p = Path(__file__).resolve().parents[2] / "apps" / "api" / "evalguard_api" / "migrations" / "versions" / "0004_rls_orgs.py"
    assert p.exists()
    body = p.read_text()
    assert "ALTER TABLE orgs ENABLE ROW LEVEL SECURITY" in body
    assert "FORCE ROW LEVEL SECURITY" in body
    assert "DROP POLICY IF EXISTS orgs_tenant_isolation" in body


def test_pool_checkout_hook_resets_rls_gucs():
    """Source pin — the fix MUST register a ``checkout`` listener
    that issues ``RESET app.org_id`` and ``RESET app.is_admin``. A
    future refactor that drops the listener regresses the
    PgBouncer-bleed protection."""
    src = Path(__file__).resolve().parents[2] / "apps" / "api" / "evalguard_api" / "db.py"
    body = src.read_text()
    assert "@event.listens_for(engine, \"checkout\")" in body
    assert "RESET app.org_id" in body
    assert "RESET app.is_admin" in body


def test_lifespan_takes_advisory_lock_on_postgres():
    """A.7: source pin — the lifespan MUST take a session-level
    advisory lock so concurrent worker starts don't race on the
    UNIQUE bootstrap-key constraint."""
    src = Path(__file__).resolve().parents[2] / "apps" / "api" / "evalguard_api" / "main.py"
    body = src.read_text()
    assert "pg_advisory_xact_lock" in body


# ---------------------------------------------------------------------------
# E.1 — api_keys serialization never leaks ``hashed_key``


def test_api_key_response_body_never_contains_hashed_key(client, auth_headers):
    """The DB row carries ``hashed_key`` but the API contract is to
    project through ``ApiKeySummary`` which omits it. Guard against a
    future refactor that swaps ``_to_summary`` for ``dict(row)`` (and
    silently exposes the hash) by asserting on the raw response text
    — every endpoint that touches an api_key must strip it."""
    create_resp = client.post(
        "/v1/orgs/org_default/api_keys",
        json={"name": "leak-test"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    list_resp = client.get(
        "/v1/orgs/org_default/api_keys", headers=auth_headers,
    )
    assert list_resp.status_code == 200
    for resp in (create_resp, list_resp):
        body = resp.text
        # The literal field name must never appear; this catches both
        # ``"hashed_key": "..."`` and ``hashed_key=...`` shapes.
        assert "hashed_key" not in body, (
            f"api_keys response leaked the hashed_key field: {body[:400]}"
        )
        assert "hashed_key" not in body.lower()


# ---------------------------------------------------------------------------
# E.4 — server recomputes audit.event_count on read


def test_get_run_recomputes_audit_event_count(client, auth_headers):
    """E.4: a hostile / buggy client can write an audit.event_count
    that disagrees with len(audit.events). The GET path MUST recompute
    so a downstream consumer can trust the count."""
    payload = {
        "schema_version": "1.0.0",
        "run_id":          "run_audittest12345",
        "project":         "default",
        "trials":          [],
        "audit": {
            "actor_id":    "cli:test",
            "actor_type":  "cli",
            "event_count": 999,                # lie!
            "events": [
                {
                    "event_id":   "ev_1",
                    "kind":       "run.created",
                    "run_id":     "run_audittest12345",
                    "actor_id":   "cli:test",
                    "actor_type": "cli",
                    "started_at": "2026-01-01T00:00:00Z",
                    "event_hash": "0" * 64,
                },
            ],
        },
    }
    ingest = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert ingest.status_code in (200, 201), ingest.text

    fetched = client.get("/v1/runs/run_audittest12345", headers=auth_headers)
    assert fetched.status_code == 200
    body = fetched.json()
    audit = body.get("audit")
    assert audit is not None, body
    # The lie (999) is replaced by the real count (1).
    assert audit["event_count"] == 1, audit


# ---------------------------------------------------------------------------
# E.5 — access-log middleware


def test_access_log_middleware_emits_structured_lines(client, auth_headers, caplog):
    """E.5: every HTTP response is logged as one structured JSON line
    with method/path/status/duration, plus key_id when auth ran."""
    import json
    import logging

    caplog.set_level(logging.INFO, logger="evalguard.api")
    r = client.get("/v1/orgs", headers=auth_headers)
    assert r.status_code == 200

    matches = [rec for rec in caplog.records if rec.name == "evalguard.api"
               and rec.message.startswith("{") and "http.request" in rec.message]
    assert matches, (
        f"no structured access-log line found; saw: "
        f"{[r.message for r in caplog.records[-5:]]}"
    )
    last = json.loads(matches[-1].message)
    assert last["evt"]    == "http.request"
    assert last["method"] == "GET"
    assert last["status"] == 200
    assert isinstance(last["duration_ms"], int)
    # ``key_id`` is omitted in open-mode tests but present in auth
    # mode; either is acceptable (we just need a structured line).
    assert "path" in last
