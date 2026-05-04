"""EvalGuard API server.

Phase 2 of the project plan. Ingests runs pushed from the CLI's
``evalguard push`` command, persists them to a relational store
(SQLite by default; Postgres-ready), and serves the same JSON
contract ``view --json`` produces under ``/v1/runs/{run_id}``.

License: Apache-2.0 (server core), separate from the MIT-licensed
CLI. ``apps/api/ee/`` (when added) will hold ELv2-licensed
enterprise modules.
"""

__version__ = "0.0.1"
