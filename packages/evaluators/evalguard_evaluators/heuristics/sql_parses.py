"""Layer-1 heuristic: model output parses as SQL in the target dialect.

A cheap pre-flight that catches the largest single class of
text-to-SQL failures (syntactically invalid SQL) without touching a
database. Uses ``sqlglot`` so any dialect it understands (postgres,
mysql, sqlite, snowflake, bigquery, redshift, …) is accepted.

``sqlglot`` is an **optional** dependency under the
``evalguard-evaluators[textsql]`` extra. The heuristic raises a clear
install hint at first use rather than at import time so the rest of
the evaluator suite still works without it.

Config:

    - type: sql_parses
      dialect: postgres            # default: parse-any (no dialect lock)
      strip_fences: true           # strip leading ```sql / trailing ``` if present
      require_select: false        # optionally enforce that the parsed root is a SELECT
"""

from __future__ import annotations

import re
from typing import Any

from evalguard_evaluators.base import EvalContext, Score


_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    """Strip a single ```sql ... ``` Markdown fence if present."""
    m = _FENCE_RE.match(text or "")
    return m.group(1) if m else text


class SqlParsesHeuristic:
    kind = "heuristic"
    layer = 1

    def __init__(self) -> None:
        self.id: str = "sql_parses"
        self._dialect: str | None = None
        self._strip_fences: bool = True
        self._require_select: bool = False

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "sql_parses")
        self._dialect = cfg.get("dialect")
        self._strip_fences = bool(cfg.get("strip_fences", True))
        self._require_select = bool(cfg.get("require_select", False))

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        try:
            import sqlglot
            import sqlglot.expressions as exp
        except ImportError as e:
            raise RuntimeError(
                "sqlglot not installed. `pip install 'evalguard-evaluators[textsql]'`"
            ) from e

        sql = _strip_code_fence(ctx.output) if self._strip_fences else (ctx.output or "")
        sql = sql.strip()
        if not sql:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "empty output"})]

        try:
            statements = sqlglot.parse(sql, read=self._dialect)
        except sqlglot.errors.ParseError as e:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "parse_error",
                           "dialect": self._dialect,
                           "error":   str(e).splitlines()[0][:240]})]

        # ``sqlglot.parse`` returns ``[None]`` for whitespace-only input
        # in some versions; treat that as a parse failure.
        statements = [s for s in statements if s is not None]
        if not statements:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "no statements parsed"})]

        if self._require_select and not isinstance(statements[0], exp.Select):
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "root is not SELECT",
                           "root_kind": type(statements[0]).__name__})]

        return [Score(self.id, self.kind, self.layer, 1.0, True,
                      {"dialect":         self._dialect,
                       "n_statements":    len(statements),
                       "root_kind":       type(statements[0]).__name__})]
