"""Regression tests for review-pass bugs fixed during the
post-2.5b code review.

Each test pins a specific bug surfaced by reading the code with a
critical eye after Phase 2.5b shipped:

- ``test_fk_enforcement_under_concurrency`` — pins the SQLite
  PRAGMA fix (foreign_keys was off on every pool connection beyond
  the initial setup one).
- ``test_bootstrap_revoke_restart_does_not_crash`` — pins the
  lifespan UNIQUE-constraint crash when ``EVALGUARD_API_KEY``
  was revoked then the server restarted.
- ``test_revoked_bootstrap_stays_revoked`` — confirms the operator
  semantics: revocation of the env key is permanent, restart does
  NOT silently recreate it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from evalguard_api.config import Settings
from evalguard_api.db import (
    create_api_key, ensure_default_tenancy, hash_token,
    key_hash_exists, make_engine,
)
from evalguard_api.main import _alembic_config

from alembic import command as alembic_command


# ---------------------------------------------------------------------------
# FK enforcement


def test_fk_enforcement_under_concurrency(tmp_path: Path):
    """The PRAGMA bug regression: every new pool connection must have
    ``foreign_keys=ON`` so the schema's CASCADE chains actually fire.

    Pre-fix: only the initial setup connection had FK on; the second
    concurrent request silently lost FK enforcement and orphaned
    rows on cascade delete.

    Test strategy: fan out 5 concurrent connections, then on each
    confirm ``PRAGMA foreign_keys`` reads as ``1`` AND that an
    intentional FK violation actually raises ``IntegrityError``.
    """
    settings = Settings(database_url=f"sqlite:///{tmp_path}/x.db")
    alembic_command.upgrade(_alembic_config(settings.database_url), "head")
    engine = make_engine(settings)

    conns = [engine.connect() for _ in range(5)]
    try:
        for i, conn in enumerate(conns):
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
            assert fk == 1, f"connection {i} has foreign_keys={fk}"
        # Active enforcement test: insert a child with a missing FK.
        # Must raise (not silently insert an orphan row).
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text("""INSERT INTO trials(
                              trial_id, run_id, project_id, provider, model,
                              row_count, row_pass_count, row_fail_count, cost_usd)
                            VALUES (
                              'trial_orphan', 'run_does_not_exist', 'proj_x',
                              'mock', 'm', 0, 0, 0, 0)"""),
                )
    finally:
        for c in conns:
            c.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Bootstrap restart-after-revoke


def test_bootstrap_revoke_restart_does_not_crash(tmp_path: Path):
    """Revoking ``EVALGUARD_API_KEY`` then restarting the server
    must not crash the lifespan.  Pre-fix, ``find_key_by_hash``
    filtered revoked rows, returned None, and the lifespan tried
    to re-INSERT — hitting the UNIQUE constraint on hashed_key
    and crashing the lifespan."""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="env-bootstrap-token",
    )
    alembic_command.upgrade(_alembic_config(settings.database_url), "head")
    engine = make_engine(settings)
    try:
        # First-startup bootstrap: create the admin key.
        with engine.begin() as conn:
            org_id, _ = ensure_default_tenancy(
                conn, org_slug="default", project_slug="default",
            )
            assert not key_hash_exists(conn, hash_token(settings.api_key))
            create_api_key(
                conn, org_id=org_id, name="bootstrap (env)",
                scopes=["admin"], token=settings.api_key,
            )

        # Operator revokes the key.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE api_keys SET revoked_at='2026-01-01' "
                     "WHERE name='bootstrap (env)'"),
            )

        # Restart bootstrap: ``key_hash_exists`` must see the row
        # (revoked) and skip re-creation. Using the post-fix path.
        with engine.begin() as conn:
            assert key_hash_exists(conn, hash_token(settings.api_key)), (
                "key_hash_exists must return True for revoked rows "
                "(otherwise the lifespan would re-INSERT and crash "
                "on UNIQUE)"
            )
    finally:
        engine.dispose()


def test_revoked_bootstrap_stays_revoked(tmp_path: Path):
    """Operator semantics check: revoke the bootstrap key, restart,
    and the revoked status must persist (no silent recreation).
    The token cannot authenticate anymore. Mirrors how secrets
    managers treat a revoked credential as dead."""
    from evalguard_api.db import find_key_by_hash, revoke_api_key

    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="env-bootstrap-token",
    )
    alembic_command.upgrade(_alembic_config(settings.database_url), "head")
    engine = make_engine(settings)
    try:
        with engine.begin() as conn:
            org_id, _ = ensure_default_tenancy(
                conn, org_slug="default", project_slug="default",
            )
            _, row = create_api_key(
                conn, org_id=org_id, name="bootstrap (env)",
                scopes=["admin"], token=settings.api_key,
            )
            key_id = row["key_id"]

        with engine.begin() as conn:
            revoke_api_key(conn, key_id)

        # Lifespan-restart logic: existence check passes (so we don't
        # crash on UNIQUE) AND we skip re-creation (so the revoke
        # sticks).
        with engine.begin() as conn:
            assert key_hash_exists(conn, hash_token(settings.api_key))
            # Active lookup returns None — token cannot auth.
            assert find_key_by_hash(conn, hash_token(settings.api_key)) is None
    finally:
        engine.dispose()
