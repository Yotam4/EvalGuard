"""Phase 2.5b plumbing — SQLAlchemy engine + Alembic migrations.

These tests don't require Postgres — they exercise the offline /
SQLite-only paths through the new infrastructure so a regression
in the engine builder, the Alembic config, or the schema metadata
fails fast without an integration setup. The end-to-end Postgres
flow is in ``test_postgres.py`` (gated by ``EVALGUARD_TEST_POSTGRES_URL``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from evalguard_api.config import Settings
from evalguard_api.db import make_engine
from evalguard_api.schema import RLS_TARGET_TABLES, metadata


# ---------------------------------------------------------------------------
# Engine builder


def test_make_engine_for_sqlite_url(tmp_path: Path):
    """The SQLite URL produces an engine whose dialect we can read
    (the rest of the codebase branches on it for RLS / pragmas)."""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/x.db")
    engine = make_engine(settings)
    assert engine.dialect.name == "sqlite"
    # Parent directory is created proactively — fresh deployments
    # don't crash because ``./.evalguard/`` doesn't exist yet.
    assert (tmp_path / "x.db").parent.exists()


def test_make_engine_creates_parent_dir(tmp_path: Path):
    """``make_engine`` mkdirs the parent of the SQLite file path so
    a fresh deployment doesn't crash on first startup."""
    nested = tmp_path / "nested" / "deeply" / "server.db"
    settings = Settings(database_url=f"sqlite:///{nested}")
    make_engine(settings)
    assert nested.parent.is_dir()


def test_make_engine_for_postgres_url_does_not_connect():
    """A Postgres URL produces an engine without actually opening a
    connection. The dialect should resolve as ``postgresql`` so
    downstream RLS branches see the right value, but we don't touch
    the network — psycopg may not even be installed for SQLite-only
    deploys."""
    settings = Settings(database_url="postgresql+psycopg://nobody@127.0.0.1:1/none")
    try:
        engine = make_engine(settings)
    except Exception as e:
        # If psycopg isn't installed the engine builder may fail at
        # dialect resolution; this is acceptable in a SQLite-only
        # deploy.  Document the symptom so a contributor sees what
        # changed.
        pytest.skip(f"psycopg not installed (skipping pg URL test): {e}")
    else:
        assert engine.dialect.name == "postgresql"


# ---------------------------------------------------------------------------
# Schema metadata


def test_metadata_declares_every_table_the_runtime_uses():
    """The runtime helpers in db.py issue raw SQL by table name; if
    a table goes missing from the SQLAlchemy ``MetaData`` (because
    a future contributor renames it without updating the source of
    truth) Alembic's autogenerate will silently drop it. This canary
    pins the canonical list."""
    expected = {
        "orgs", "projects", "api_keys",
        "runs", "trials", "run_rows", "gate_results", "assets", "events",
    }
    actual = set(metadata.tables.keys())
    assert expected <= actual, f"missing from MetaData: {expected - actual}"


def test_rls_target_tables_subset_of_metadata():
    """Every entry in the RLS target list must correspond to a real
    table — typos here would silently disable RLS on some tables in
    Phase 2.5b's migration 0002."""
    table_names = set(metadata.tables.keys())
    for tbl in RLS_TARGET_TABLES:
        assert tbl in table_names, f"RLS target {tbl!r} not in metadata"


# ---------------------------------------------------------------------------
# Alembic — migrations apply cleanly on a fresh SQLite DB


def test_alembic_upgrade_head_creates_all_tables(client):
    """Running the lifespan (which calls alembic upgrade head) must
    leave the schema fully populated. The ``client`` fixture wires
    the lifespan; we just check the result via SQLAlchemy inspect.

    On SQLite, ``0002_rls_policies.py`` and ``0004_rls_orgs.py`` are
    no-ops so this also proves the dialect-guard fires correctly."""
    engine = client.app.state.engine
    insp = inspect(engine)
    actual = set(insp.get_table_names())
    expected = {
        "orgs", "projects", "api_keys",
        "runs", "trials", "run_rows", "gate_results", "assets", "events",
        "row_reviews",        # Phase 4
        "golden_candidates",  # Phase OBS-4
        "alembic_version",    # Alembic's own bookkeeping table
    }
    assert expected <= actual, f"missing tables after upgrade: {expected - actual}"


def test_alembic_version_recorded(client):
    """``alembic_version`` should carry the head revision id after
    upgrade. Re-startups should be no-ops (this fixture rebuilds the
    app each test, but the ``alembic_version`` row is what proves
    the migration ran end-to-end)."""
    from sqlalchemy import text
    engine = client.app.state.engine
    with engine.connect() as conn:
        ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    # Head bumps as we ship migrations.  ``0012_event_rows`` adds the
    # per-event audit chain for proxy ingest — PROXY-3.5 ticket #8.
    assert ver == "0012_event_rows"
