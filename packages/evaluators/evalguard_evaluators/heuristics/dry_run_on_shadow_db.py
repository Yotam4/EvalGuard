"""Layer-1 heuristic: model output executes against a shadow database.

Catches the second-largest text-to-SQL failure class (queries that
parse but reference missing tables/columns, or otherwise blow up at
execution time) without touching the production database.

The "shadow" database is configured via the top-level ``systems:``
block; the heuristic looks it up by ``system: <name>``. Today only
``kind: sqlite`` is built in (uses Python's stdlib ``sqlite3`` — no
extra install required); other dialects can be added as plugins under
the same evaluator id.

Config:

    systems:
      shadow:
        kind:    sqlite
        url:     "sqlite:///./.evalguard/shadow.db"   # or :memory:
        schema:  schemas/db.sql                       # optional DDL bootstrap

    heuristics:
      - type: dry_run_on_shadow_db
        system: shadow
        strip_fences: true
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from evalguard_evaluators.base import EvalContext, Score


_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    return m.group(1) if m else text


def _sqlite_path_from_url(url: str) -> str:
    """Translate ``sqlite:///path/to.db`` (SQLAlchemy-style URL) to a
    plain filesystem path, with ``:memory:`` left alone."""
    if not url or url == ":memory:":
        return ":memory:"
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):] or ":memory:"
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):] or ":memory:"
    return url


class DryRunOnShadowDbHeuristic:
    kind = "heuristic"
    layer = 1

    def __init__(self) -> None:
        self.id: str = "dry_run_on_shadow_db"
        self._system: dict[str, Any] = {}
        self._strip_fences: bool = True

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "dry_run_on_shadow_db")
        self._strip_fences = bool(cfg.get("strip_fences", True))
        # ``_system`` is inlined by the YAML loader from the top-level
        # systems: block. Without it the heuristic can't run.
        sys_cfg = cfg.get("_system") or {}
        if not sys_cfg:
            raise ValueError(
                f"{self.id}: missing 'system: <name>' or top-level systems[<name>]"
            )
        self._system = sys_cfg

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        kind = (self._system.get("kind") or "").lower()
        if kind != "sqlite":
            raise RuntimeError(
                f"{self.id}: built-in shadow DB only supports kind=sqlite, "
                f"got {kind!r}. Add a plugin to extend."
            )
        sql = _strip_fence(ctx.output) if self._strip_fences else (ctx.output or "")
        sql = sql.strip().rstrip(";")
        if not sql:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "empty output"})]

        path = _sqlite_path_from_url(self._system.get("url") or ":memory:")
        try:
            conn = _connect_and_seed(path, self._system.get("schema"))
        except sqlite3.Error as e:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "shadow_db_setup_failed",
                           "error":  str(e)[:240]})]
        try:
            cur = conn.execute(sql)
            cur.fetchall()
            return [Score(self.id, self.kind, self.layer, 1.0, True,
                          {"system_kind": "sqlite",
                           "system_name": self._system.get("name")})]
        except sqlite3.Error as e:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason":      "execution_error",
                           "system_kind": "sqlite",
                           "error":       str(e)[:240]})]
        finally:
            conn.close()


def _connect_and_seed(path: str, schema_blob: str | None) -> sqlite3.Connection:
    """Open ``path`` (SQLite file or :memory:), apply DDL if provided.

    DDL is run inside a single executescript() so multi-statement
    bootstrap works. Errors propagate to the caller as sqlite3.Error.
    """
    if path != ":memory:":
        # Make sure the parent dir exists for fresh shadow files.
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if schema_blob:
        conn.executescript(schema_blob)
        conn.commit()
    return conn
