"""Database layer: SQLAlchemy engine + dialect-portable helpers.

Phase 2.5b moved this layer from raw ``sqlite3`` to SQLAlchemy 2.0
core. Every helper here speaks SQL via ``text()`` with named (``:foo``)
parameters so the same function works against SQLite (default) or
Postgres (the ``[postgres]`` install extra) with no branching at
the call site.

Why core ``text()`` instead of constructed ``select()`` / ``insert()``:

- The ingest path is procedural: write a run row, fan out to N
  trial rows, fan out to N×M score rows. Imperative SQL is cleaner
  than building Insert objects in a loop.
- ``text()`` queries are inspectable in logs / EXPLAIN with no
  surprise transformations.
- We still get parameter binding (no string interpolation → no SQL
  injection) and dialect-appropriate type conversion.

Connection lifecycle:

- One ``Engine`` per app instance, built in the lifespan with
  ``make_engine(settings)``. The engine owns the connection pool.
- Per-request work uses ``engine.begin()`` (autocommit transaction)
  or ``engine.connect()`` (read-only) inside a ``with`` block so
  connections always return to the pool.
- On Postgres, a request handler that acts on per-tenant data calls
  ``apply_rls_context(conn, principal)`` at the start of the
  transaction; the policies in migration 0002 then enforce
  visibility from the database side.
"""

from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

if TYPE_CHECKING:
    from evalguard_api.config import Settings


# ---------------------------------------------------------------------------
# Engine + connection helpers


def make_engine(settings: "Settings") -> Engine:
    """Build the per-app ``Engine`` from a ``Settings`` snapshot.

    For SQLite, mkdir the parent directory and turn on WAL +
    ``synchronous=NORMAL`` for the same concurrent-read profile the
    Phase-1 ``connect()`` provided.

    For Postgres, the URL flows through unchanged; SQLAlchemy picks
    psycopg3 by default (the ``[postgres]`` extra installs it).
    """
    url = settings.database_url
    if url.startswith("sqlite:///"):
        # Mkdir parent so a fresh deployment doesn't crash on startup.
        path = url[len("sqlite:///"):]
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url,
            # ``check_same_thread=False`` is safe with SQLAlchemy's
            # connection pool — the pool serializes per-connection use.
            connect_args={"check_same_thread": False, "timeout": 30.0},
        )
        # WAL + synchronous=NORMAL on every new connection. Pragmas
        # are per-connection on SQLite; the pool may hand the same
        # connection out repeatedly, so once-set is enough but
        # idempotent re-application costs nothing.
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
        return engine
    # Postgres / anything-SQLAlchemy-supports.
    return create_engine(url, future=True)


def now_iso() -> str:
    """ISO-8601 UTC with microsecond precision.

    Microseconds (vs the second-precision used in the CLI) means
    two consecutive ingests in the same wall-clock second still get
    distinct ``ingested_at`` values, so the run-listing endpoint's
    ``ORDER BY ingested_at DESC`` is a stable sort without needing
    a backend-specific tiebreaker (sqlite ``rowid`` doesn't translate
    to Postgres).
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@contextmanager
def begin(engine: Engine) -> Iterator[Connection]:
    """``engine.begin()`` as a context manager.

    Trivially thin — exists so call sites read as ``with begin(engine)
    as conn`` rather than the noisier raw form.
    """
    with engine.begin() as conn:
        yield conn


@contextmanager
def connect(engine: Engine) -> Iterator[Connection]:
    """``engine.connect()`` as a context manager (read-only callers)."""
    with engine.connect() as conn:
        yield conn


# ---------------------------------------------------------------------------
# RLS context — set per-transaction so policies in migration 0002 can
# enforce tenancy at the database layer (defense-in-depth on top of
# the application-layer auth in ``auth.py``).


def apply_rls_context(conn: Connection, *, org_id: str, is_admin: bool) -> None:
    """Set ``app.org_id`` and ``app.is_admin`` for the current
    Postgres transaction. No-op on SQLite (no RLS support).

    Must be called inside a transaction (``engine.begin()``) so the
    ``SET LOCAL`` is bounded to that transaction. ``set_config(...,
    is_local=true)`` does the same thing as ``SET LOCAL`` but works
    cleanly with parameter binding.
    """
    if conn.engine.dialect.name != "postgresql":
        return
    conn.execute(
        text("SELECT set_config('app.org_id', :v, true)"),
        {"v": org_id},
    )
    conn.execute(
        text("SELECT set_config('app.is_admin', :v, true)"),
        {"v": "1" if is_admin else "0"},
    )


# ---------------------------------------------------------------------------
# Tenancy bootstrap


def ensure_default_tenancy(
    conn: Connection, *, org_slug: str, project_slug: str,
) -> tuple[str, str]:
    """Make sure the default org + project exist; return their ids.

    Idempotent: re-startups don't churn. Called from the lifespan
    after Alembic has created the tables.
    """
    org_row = conn.execute(
        text("SELECT org_id FROM orgs WHERE slug = :slug"),
        {"slug": org_slug},
    ).fetchone()
    if org_row is None:
        org_id = f"org_{org_slug}"
        conn.execute(
            text("INSERT INTO orgs(org_id, slug, name, created_at) "
                 "VALUES (:org_id, :slug, :name, :created_at)"),
            {"org_id": org_id, "slug": org_slug,
             "name": org_slug.title(), "created_at": now_iso()},
        )
    else:
        org_id = org_row[0]

    proj_row = conn.execute(
        text("SELECT project_id FROM projects WHERE org_id=:org_id AND slug=:slug"),
        {"org_id": org_id, "slug": project_slug},
    ).fetchone()
    if proj_row is None:
        project_id = f"proj_{project_slug}"
        conn.execute(
            text("INSERT INTO projects(project_id, org_id, slug, name, created_at) "
                 "VALUES (:project_id, :org_id, :slug, :name, :created_at)"),
            {"project_id": project_id, "org_id": org_id,
             "slug": project_slug, "name": project_slug.title(),
             "created_at": now_iso()},
        )
    else:
        project_id = proj_row[0]
    return org_id, project_id


# ---------------------------------------------------------------------------
# API-key plaintext / hash helpers
#
# Token shape: ``evk_<32 hex>``. The prefix is searchable so secret
# scanners (GitHub, gitleaks, trufflehog) catch leaks; the entropy
# is 128 bits, which is overkill for a bearer but cheap.
#
# Storage: only ``sha256(token)`` lands in the DB. The plaintext is
# returned to the caller exactly once (POST response on creation) and
# never recoverable again — losing it forces a regenerate.


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
    the UI without exposing the secret. ``evk_a1b2c3d4`` is the
    typical result."""
    return token[: len(_TOKEN_PREFIX) + 8]


# ---------------------------------------------------------------------------
# API-key persistence


def find_key_by_hash(conn: Connection, hashed: str) -> dict | None:
    """Look up an active api_key by its hash. Returns None for unknown
    or revoked keys (revoked_at not null). Touches ``last_used_at`` on
    hit so an operator can see which keys are live traffic."""
    row = conn.execute(
        text("""SELECT key_id, org_id, prefix, name, scopes_csv,
                       created_at, revoked_at, last_used_at
                FROM api_keys
                WHERE hashed_key=:hashed AND revoked_at IS NULL"""),
        {"hashed": hashed},
    ).mappings().fetchone()
    if row is None:
        return None
    conn.execute(
        text("UPDATE api_keys SET last_used_at=:t WHERE key_id=:k"),
        {"t": now_iso(), "k": row["key_id"]},
    )
    return dict(row)


def create_api_key(
    conn: Connection,
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
        text("""INSERT INTO api_keys(
                  key_id, org_id, prefix, hashed_key, name, scopes_csv,
                  created_at, revoked_at, last_used_at)
                VALUES (:key_id, :org_id, :prefix, :hashed_key, :name, :scopes_csv,
                        :created_at, NULL, NULL)"""),
        {
            "key_id":     key_id,
            "org_id":     org_id,
            "prefix":     token_prefix(plaintext),
            "hashed_key": hashed,
            "name":       name,
            "scopes_csv": scopes_csv,
            "created_at": now_iso(),
        },
    )
    row = conn.execute(
        text("SELECT * FROM api_keys WHERE key_id=:k"), {"k": key_id},
    ).mappings().fetchone()
    return plaintext, dict(row)


def list_api_keys_for_org(conn: Connection, org_id: str) -> list[dict]:
    rows = conn.execute(
        text("""SELECT key_id, org_id, prefix, name, scopes_csv,
                       created_at, revoked_at, last_used_at
                FROM api_keys
                WHERE org_id=:org_id
                ORDER BY created_at DESC"""),
        {"org_id": org_id},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(conn: Connection, key_id: str) -> bool:
    """Mark a key revoked. Returns True if a row was affected."""
    result = conn.execute(
        text("UPDATE api_keys SET revoked_at=:t "
             "WHERE key_id=:k AND revoked_at IS NULL"),
        {"t": now_iso(), "k": key_id},
    )
    return result.rowcount > 0


def get_api_key(conn: Connection, key_id: str) -> dict | None:
    row = conn.execute(
        text("SELECT * FROM api_keys WHERE key_id=:k"), {"k": key_id},
    ).mappings().fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Org / Project CRUD


def create_org(conn: Connection, *, slug: str, name: str) -> dict:
    org_id = f"org_{slug}"
    conn.execute(
        text("INSERT INTO orgs(org_id, slug, name, created_at) "
             "VALUES (:org_id, :slug, :name, :created_at)"),
        {"org_id": org_id, "slug": slug, "name": name, "created_at": now_iso()},
    )
    row = conn.execute(
        text("SELECT * FROM orgs WHERE org_id=:org_id"),
        {"org_id": org_id},
    ).mappings().fetchone()
    return dict(row)


def get_org(conn: Connection, org_id: str) -> dict | None:
    row = conn.execute(
        text("SELECT * FROM orgs WHERE org_id=:org_id"), {"org_id": org_id},
    ).mappings().fetchone()
    return dict(row) if row else None


def get_org_by_slug(conn: Connection, slug: str) -> dict | None:
    row = conn.execute(
        text("SELECT * FROM orgs WHERE slug=:slug"), {"slug": slug},
    ).mappings().fetchone()
    return dict(row) if row else None


def list_orgs(conn: Connection) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM orgs ORDER BY created_at"),
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def upsert_project(
    conn: Connection, *, org_id: str, project_name: str,
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
        text("SELECT project_id FROM projects WHERE org_id=:org AND slug=:slug"),
        {"org": org_id, "slug": project_name},
    ).fetchone()
    if row is not None:
        return row[0]
    project_id = "proj_" + secrets.token_hex(8)
    conn.execute(
        text("INSERT INTO projects(project_id, org_id, slug, name, created_at) "
             "VALUES (:project_id, :org_id, :slug, :name, :created_at)"),
        {"project_id": project_id, "org_id": org_id,
         "slug": project_name, "name": project_name,
         "created_at": now_iso()},
    )
    return project_id


def create_project_explicit(
    conn: Connection,
    *,
    org_id: str,
    slug: str,
    name: str | None = None,
) -> dict:
    """Project create with explicit slug + name. ``upsert_project``
    is the run-ingest-driven path; this is the user-facing
    ``POST /v1/projects`` path."""
    project_id = "proj_" + secrets.token_hex(8)
    conn.execute(
        text("INSERT INTO projects(project_id, org_id, slug, name, created_at) "
             "VALUES (:project_id, :org_id, :slug, :name, :created_at)"),
        {"project_id": project_id, "org_id": org_id,
         "slug": slug, "name": name or slug,
         "created_at": now_iso()},
    )
    row = conn.execute(
        text("SELECT * FROM projects WHERE project_id=:p"),
        {"p": project_id},
    ).mappings().fetchone()
    return dict(row)


def get_project_by_slug(
    conn: Connection, *, org_id: str, slug: str,
) -> dict | None:
    row = conn.execute(
        text("SELECT * FROM projects WHERE org_id=:org AND slug=:slug"),
        {"org": org_id, "slug": slug},
    ).mappings().fetchone()
    return dict(row) if row else None


def list_projects_for_org(conn: Connection, org_id: str) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM projects WHERE org_id=:org ORDER BY created_at"),
        {"org": org_id},
    ).mappings().fetchall()
    return [dict(r) for r in rows]
