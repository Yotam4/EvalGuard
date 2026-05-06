"""Shared FastAPI dependencies — connection / transaction lifecycle.

A request handler that needs the database asks for ``conn`` (a
SQLAlchemy ``Connection``); this module wraps the engine, opens a
transaction, sets the per-tenant Postgres GUCs that drive RLS, and
yields the connection. The contextmanager pattern guarantees the
transaction commits on a clean return and rolls back on a raised
exception — same lifecycle as the Phase-1 ``with engine.begin()``
block, just hoisted into a dependency so every route can share it.

The dependency takes an explicit ``Principal`` so the RLS context
varies per caller, not per app instance. Without this the policies
would have nothing to compare against.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Request
from sqlalchemy.engine import Connection

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import apply_rls_context


def get_conn(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Iterator[Connection]:
    """Yield a Connection inside an open transaction with RLS context
    set. Commits on a clean return, rolls back on exceptions.

    The transaction wrapping is intentional even for read-only
    handlers: ``SET LOCAL`` (and ``set_config(..., is_local=true)``)
    only persists for the current transaction, so reading inside a
    transaction is the only way to keep the GUC scoped.
    """
    engine = request.app.state.engine
    with engine.begin() as conn:
        apply_rls_context(conn, org_id=principal.org_id, is_admin=principal.is_admin)
        yield conn
