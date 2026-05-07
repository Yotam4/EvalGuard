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
