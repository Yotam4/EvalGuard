"""SQLite-backed storage for the API server.

Schema mirrors the CLI's local-executor tables but adds
multi-tenancy: every domain row carries ``project_id``, every
project carries ``org_id``. The result is a single-database, single-
tenant-by-default deployment that's structurally ready to grow into
multi-tenant Postgres without schema redesign.

Why stdlib ``sqlite3`` and not SQLAlchemy:

- Matches the CLI; one mental model.
- Zero extra dependency on the install footprint.
- The Postgres port (Phase 2.5) is the right time to introduce
  SQLAlchemy + Alembic — until then a hand-written schema is
  cheaper than carrying ORM ceremony.

Concurrency: SQLite + ``check_same_thread=False`` + WAL is fine for
the read-heavy / occasionally-write workload of an ingestion API
sitting behind a small uvicorn pool. Each request gets its own
connection from a per-request dependency.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_SCHEMA = """
PRAGMA foreign_keys = ON;

-- Tenancy ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orgs (
  org_id      TEXT PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  project_id  TEXT PRIMARY KEY,
  org_id      TEXT NOT NULL REFERENCES orgs(org_id),
  slug        TEXT NOT NULL,
  name        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  UNIQUE(org_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id);

-- Per-org API keys. The token plaintext is **never** stored — only its
-- sha256 hash. ``prefix`` keeps the first 12 chars of the token (e.g.
-- ``evk_a1b2c3d4``) so an operator can identify a key in the UI / logs
-- without exposing the secret. ``name`` is a human label ("ci-prod",
-- "alice-laptop", …) so listing returns something useful.
--
-- ``scopes_csv`` is a comma-separated list of scope strings.  Empty
-- string means "org-scoped only" — i.e. the key can act on its
-- ``org_id`` and nothing else.  ``admin`` means "cross-org": typically
-- assigned to the bootstrap key materialized from ``EVALGUARD_API_KEY``.
CREATE TABLE IF NOT EXISTS api_keys (
  key_id          TEXT PRIMARY KEY,
  org_id          TEXT NOT NULL REFERENCES orgs(org_id),
  prefix          TEXT NOT NULL,
  hashed_key      TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  scopes_csv      TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  revoked_at      TEXT,
  last_used_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_org   ON api_keys(org_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash  ON api_keys(hashed_key);

-- Run-shape ----------------------------------------------------------------
-- Tables mirror the CLI's local schema 1:1 (so ``serializer.run_to_dict``
-- output ports directly). Every table picks up a ``project_id`` so a
-- single physical database can host multiple isolated projects.

CREATE TABLE IF NOT EXISTS runs (
  run_id          TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(project_id),
  project_name    TEXT NOT NULL,
  -- ``config_hash`` may legitimately be absent on minimal pushes
  -- (e.g. an audit-only ingest replayed from a foreign archive),
  -- so it's nullable. The CLI always supplies it on real runs.
  config_hash     TEXT,
  status          TEXT,
  row_status      TEXT,
  gate_status     TEXT,
  started_at      TEXT,
  finished_at     TEXT,
  cost_usd        REAL DEFAULT 0,
  row_count       INTEGER DEFAULT 0,
  row_pass_count  INTEGER DEFAULT 0,
  row_fail_count  INTEGER DEFAULT 0,
  payload_json    TEXT NOT NULL,        -- the canonical run_to_dict() output
  ingested_at     TEXT NOT NULL,
  ingested_by     TEXT                  -- key_id or 'open-mode'
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs(status);

-- For querying without parsing payload_json. Mirror the CLI tables but
-- denormalize project_id so a project filter on /v1/runs/{...}/rows is
-- cheap.

CREATE TABLE IF NOT EXISTS trials (
  trial_id          TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  project_id        TEXT NOT NULL,
  provider_id       TEXT,
  provider          TEXT,
  model             TEXT,
  prompt_id         TEXT,
  prompt_version_id TEXT,
  row_count         INTEGER DEFAULT 0,
  row_pass_count    INTEGER DEFAULT 0,
  row_fail_count    INTEGER DEFAULT 0,
  cost_usd          REAL DEFAULT 0,
  status            TEXT,
  gate_status       TEXT,
  started_at        TEXT,
  finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(run_id);

CREATE TABLE IF NOT EXISTS run_rows (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  trial_id     TEXT NOT NULL,
  project_id   TEXT NOT NULL,
  row_id       TEXT NOT NULL,
  passed       INTEGER NOT NULL,
  n_scores     INTEGER NOT NULL,
  cost_usd     REAL DEFAULT 0,
  latency_ms   INTEGER DEFAULT 0,
  cache_hit    INTEGER DEFAULT 0,
  tags_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_rows_run   ON run_rows(run_id);
CREATE INDEX IF NOT EXISTS idx_run_rows_trial ON run_rows(trial_id);

CREATE TABLE IF NOT EXISTS gate_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  trial_id      TEXT,
  project_id    TEXT NOT NULL,
  gate_name     TEXT NOT NULL,
  blocking      INTEGER,
  passed        INTEGER NOT NULL,
  severity      TEXT,
  layer         INTEGER,
  details_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_gate_run ON gate_results(run_id);

CREATE TABLE IF NOT EXISTS assets (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  project_id  TEXT NOT NULL,
  kind        TEXT NOT NULL,
  asset_id    TEXT NOT NULL,
  version_id  TEXT NOT NULL,
  source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_run ON assets(run_id);

-- Audit events stored as a JSON blob for now; full per-event indexing
-- and chain re-verification on the server side is a separate phase.

CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  project_id    TEXT NOT NULL,
  event_count   INTEGER NOT NULL,
  chain_tip     TEXT,
  events_json   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the conventions used throughout the
    server: WAL for concurrent reads, foreign keys on, busy timeout
    so concurrent writes wait rather than fail.

    Pass ``:memory:`` for tests; pass a filesystem path otherwise.
    """
    if isinstance(path, str) and path == ":memory:":
        target: str = ":memory:"
    else:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        target = str(p)
    conn = sqlite3.connect(target, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema. Idempotent — every CREATE is IF NOT EXISTS."""
    conn.executescript(_SCHEMA)
    conn.commit()


def ensure_default_tenancy(
    conn: sqlite3.Connection, *, org_slug: str, project_slug: str,
) -> tuple[str, str]:
    """Make sure the default org + project exist; return their ids.

    Called from the FastAPI lifespan on startup. Idempotent so server
    restarts don't churn the table.
    """
    org_row = conn.execute(
        "SELECT org_id FROM orgs WHERE slug=?", (org_slug,),
    ).fetchone()
    if org_row is None:
        org_id = f"org_{org_slug}"
        conn.execute(
            "INSERT INTO orgs(org_id, slug, name, created_at) VALUES (?,?,?,?)",
            (org_id, org_slug, org_slug.title(), now_iso()),
        )
    else:
        org_id = org_row["org_id"]
    proj_row = conn.execute(
        "SELECT project_id FROM projects WHERE org_id=? AND slug=?",
        (org_id, project_slug),
    ).fetchone()
    if proj_row is None:
        project_id = f"proj_{project_slug}"
        conn.execute(
            "INSERT INTO projects(project_id, org_id, slug, name, created_at) VALUES (?,?,?,?,?)",
            (project_id, org_id, project_slug, project_slug.title(), now_iso()),
        )
    else:
        project_id = proj_row["project_id"]
    conn.commit()
    return org_id, project_id


def upsert_project(
    conn: sqlite3.Connection, *, org_id: str, project_name: str,
) -> str:
    """Look up (or create) a project by ``project_name`` within ``org_id``.

    The CLI sends ``project: "<name>"`` in every run payload; the
    server uses that name verbatim as the slug.  Lookup is by
    composite ``(org_id, slug)`` — the project_id PK itself is a
    random opaque id so two orgs may have a project with the same
    slug without colliding on the global PK.

    Returns ``project_id``.
    """
    row = conn.execute(
        "SELECT project_id FROM projects WHERE org_id=? AND slug=?",
        (org_id, project_name),
    ).fetchone()
    if row is not None:
        return row["project_id"]
    import secrets as _secrets   # local import keeps stdlib usage close to call site
    project_id = "proj_" + _secrets.token_hex(8)
    conn.execute(
        "INSERT INTO projects(project_id, org_id, slug, name, created_at) VALUES (?,?,?,?,?)",
        (project_id, org_id, project_name, project_name, now_iso()),
    )
    conn.commit()
    return project_id


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a body in a transaction. Rolls back on any exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# API-key plaintext / hash helpers.
#
# Token shape: ``evk_<32 hex>``. The prefix is searchable so secret
# scanners (GitHub, gitleaks, trufflehog) catch leaks; the entropy
# is 128 bits which is overkill for a bearer but cheap.
#
# Storage: only ``sha256(token)`` lands in the DB. The plaintext is
# returned to the caller exactly once (POST response on creation) and
# never recoverable again — losing it forces a regenerate.

import hashlib  # noqa: E402 — placed here so the helpers below are co-located
import secrets  # noqa: E402

_TOKEN_PREFIX = "evk_"
_TOKEN_RANDOM_BYTES = 16  # 32 hex chars after the prefix


def generate_token() -> str:
    """Generate a fresh bearer token. Returned to the caller exactly
    once; the server only persists ``hash_token(token)``."""
    return _TOKEN_PREFIX + secrets.token_hex(_TOKEN_RANDOM_BYTES)


def hash_token(token: str) -> str:
    """Stable hash of a token suitable for ``api_keys.hashed_key``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """First ``len('evk_') + 8`` chars — enough to identify a key in
    the UI without exposing the secret. ``evk_a1b2c3d4`` is the result
    for the typical token."""
    return token[: len(_TOKEN_PREFIX) + 8]


# ---------------------------------------------------------------------------
# API-key persistence


def find_key_by_hash(
    conn: sqlite3.Connection, hashed: str,
) -> dict | None:
    """Look up an active api_key by its hash. Returns None for unknown
    or revoked keys (revoked_at not null). Touches ``last_used_at`` on
    hit so an operator can see which keys are live traffic."""
    row = conn.execute(
        """SELECT key_id, org_id, prefix, name, scopes_csv,
                  created_at, revoked_at, last_used_at
           FROM api_keys WHERE hashed_key=? AND revoked_at IS NULL""",
        (hashed,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE api_keys SET last_used_at=? WHERE key_id=?",
        (now_iso(), row["key_id"]),
    )
    conn.commit()
    return dict(row)


def create_api_key(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    name: str,
    scopes: list[str] | None = None,
    token: str | None = None,
) -> tuple[str, dict]:
    """Create a new api_key row. Returns ``(plaintext_token, key_row)``.

    ``token`` is normally generated server-side; tests / bootstrap
    pass an explicit value to make the hash deterministic.
    """
    plaintext = token or generate_token()
    hashed = hash_token(plaintext)
    key_id = "key_" + secrets.token_hex(8)
    scopes_csv = ",".join(s.strip() for s in (scopes or []) if s.strip())
    conn.execute(
        """INSERT INTO api_keys(
              key_id, org_id, prefix, hashed_key, name, scopes_csv,
              created_at, revoked_at, last_used_at)
           VALUES (?,?,?,?,?,?,?,NULL,NULL)""",
        (
            key_id, org_id, token_prefix(plaintext), hashed,
            name, scopes_csv, now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_id=?", (key_id,),
    ).fetchone()
    return plaintext, dict(row)


def list_api_keys_for_org(
    conn: sqlite3.Connection, org_id: str,
) -> list[dict]:
    rows = conn.execute(
        """SELECT key_id, org_id, prefix, name, scopes_csv,
                  created_at, revoked_at, last_used_at
           FROM api_keys WHERE org_id=?
           ORDER BY created_at DESC""",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(
    conn: sqlite3.Connection, key_id: str,
) -> bool:
    """Mark a key revoked. Returns True if a row was affected."""
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
        (now_iso(), key_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_api_key(
    conn: sqlite3.Connection, key_id: str,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_id=?", (key_id,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Org / Project CRUD


def create_org(
    conn: sqlite3.Connection, *, slug: str, name: str,
) -> dict:
    org_id = f"org_{slug}"
    conn.execute(
        "INSERT INTO orgs(org_id, slug, name, created_at) VALUES (?,?,?,?)",
        (org_id, slug, name, now_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM orgs WHERE org_id=?", (org_id,),
    ).fetchone()
    return dict(row)


def get_org(conn: sqlite3.Connection, org_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM orgs WHERE org_id=?", (org_id,),
    ).fetchone()
    return dict(row) if row else None


def get_org_by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM orgs WHERE slug=?", (slug,),
    ).fetchone()
    return dict(row) if row else None


def list_orgs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM orgs ORDER BY created_at",
    ).fetchall()
    return [dict(r) for r in rows]


def create_project_explicit(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    slug: str,
    name: str | None = None,
) -> dict:
    """Project create with explicit slug + name. ``upsert_project``
    above is the run-ingest-driven path; this is the user-facing
    ``POST /v1/projects`` path. Like upsert_project, uses a random
    opaque ``project_id`` so the (org_id, slug) composite is the
    real uniqueness boundary."""
    project_id = "proj_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO projects(project_id, org_id, slug, name, created_at) "
        "VALUES (?,?,?,?,?)",
        (project_id, org_id, slug, name or slug, now_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    return dict(row)


def get_project_by_slug(
    conn: sqlite3.Connection, *, org_id: str, slug: str,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE org_id=? AND slug=?",
        (org_id, slug),
    ).fetchone()
    return dict(row) if row else None


def list_projects_for_org(
    conn: sqlite3.Connection, org_id: str,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM projects WHERE org_id=? ORDER BY created_at",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]
