"""Alembic environment.

Reads the database URL from ``EVALGUARD_DATABASE_URL`` (same env var
the runtime uses) so a single ``alembic upgrade head`` invocation
works across local SQLite, staging Postgres, and prod Postgres
without config-per-deployment.

``target_metadata`` is the ``MetaData`` from
``evalguard_api/schema.py`` so future migrations generated with
``alembic revision --autogenerate`` diff against the canonical
schema.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# This file is loaded by Alembic; importing the schema module brings
# the ``MetaData`` into scope for autogenerate.
from evalguard_api.schema import metadata as target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the URL from the env var if set. ``alembic upgrade head``
# in a Docker container will pick up ``EVALGUARD_DATABASE_URL``
# automatically; local invocations can pass ``-x url=...`` instead.
_env_url = os.environ.get("EVALGUARD_DATABASE_URL")
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)
elif not config.get_main_option("sqlalchemy.url"):
    # Sensible default that matches the runtime — local SQLite under
    # ``./.evalguard/server.db``. Mirrors ``Settings`` defaults.
    config.set_main_option("sqlalchemy.url", "sqlite:///./.evalguard/server.db")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # ``render_as_batch`` makes SQLite's ALTER-less limitations
        # invisible to migration authors — Alembic emits the
        # copy-rename dance under the hood.
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live engine."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            render_as_batch=conn.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
