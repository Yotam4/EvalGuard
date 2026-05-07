"""Layer-1 heuristic: candidate SQL produces the same result set as
the expected SQL when both are run against the shadow database.

This is the strongest deterministic signal for text-to-SQL eval: it
catches the silent-wrong-result class (off-by-one bounds, wrong
``GROUP BY``, missing ``DISTINCT``) that a parse-only check or an LLM
intent-judge can miss.

Comparison strategy:

- Default: order-insensitive (rows compared as a multi-set of tuples).
- ``ordered: true``: order-sensitive (rows compared as a list).
- ``columns_strict: true``: column count must match (default loose:
  candidate may have more columns and still match if the leading ones
  agree). Rarely useful — included for symmetry with the SQL semantic
  equivalence options users will recognize.

Source of truth for "expected":

- ``ctx.extra['expected_result']`` if present (precomputed result set
  as a list of lists / list of dicts) — fastest, no shadow execution
  of the expected query needed.
- Otherwise ``ctx.extra['expected_sql']`` is run on the shadow DB.
- Otherwise the heuristic skips with ``passed=True`` and a warning,
  so a partially-annotated golden set doesn't tank the gate.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from typing import Any

from evalguard_evaluators.base import EvalContext, Score
from evalguard_evaluators.heuristics.dry_run_on_shadow_db import (
    _connect_and_seed, _sqlite_path_from_url,
)


_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    return m.group(1) if m else text


def _coerce_cell(value: Any) -> Any:
    """Normalize a single cell so semantically-equal values across
    backends compare equal:

    - ``2.0`` (float, sqlite SUM) ↔ ``2`` (int, JSON literal): both
      become int when the float is whole.
    - ``Decimal('2.5')`` (Postgres NUMERIC) ↔ ``2.5`` (JSON float):
      Decimal coerced to float.
    - ``datetime`` / ``date`` left alone — caller compares them
      as-is; if a future need arises we'll canonicalize to ISO.
    """
    # Avoid importing decimal at module load — most rows don't carry it.
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            f = float(value)
            return int(f) if f.is_integer() else f
    except Exception:  # noqa: BLE001 — coercion is best-effort
        pass
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
    return value


def _normalize_rows(rows: list[Any]) -> list[tuple]:
    """Coerce rows into tuples so they compare cleanly across shapes
    (sqlite3.Row, list, tuple, dict) AND across types (int/float/
    Decimal — see ``_coerce_cell``)."""
    out: list[tuple] = []
    for r in rows:
        if isinstance(r, dict):
            cells = tuple(_coerce_cell(r[k]) for k in sorted(r))
        elif isinstance(r, (list, tuple)):
            cells = tuple(_coerce_cell(c) for c in r)
        else:
            cells = (_coerce_cell(r),)
        out.append(cells)
    return out


class ResultSetEquivalenceHeuristic:
    kind = "heuristic"
    layer = 1

    def __init__(self) -> None:
        self.id: str = "result_set_equivalence"
        self._system: dict[str, Any] = {}
        self._ordered: bool = False
        self._columns_strict: bool = False
        self._strip_fences: bool = True

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "result_set_equivalence")
        self._ordered = bool(cfg.get("ordered", False))
        self._columns_strict = bool(cfg.get("columns_strict", False))
        self._strip_fences = bool(cfg.get("strip_fences", True))
        sys_cfg = cfg.get("_system") or {}
        if not sys_cfg:
            raise ValueError(
                f"{self.id}: missing 'system: <name>' or top-level systems[<name>]"
            )
        # Fail-fast on misconfigured kind (was per-row).
        kind = (sys_cfg.get("kind") or "").lower()
        if kind != "sqlite":
            raise ValueError(
                f"{self.id}: built-in shadow DB only supports kind=sqlite, got {kind!r}."
            )
        self._system = sys_cfg

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        candidate = _strip_fence(ctx.output) if self._strip_fences else (ctx.output or "")
        candidate = candidate.strip().rstrip(";")
        if not candidate:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "empty candidate output"})]

        # Find the expected result-set or expected query.
        extra = ctx.extra if isinstance(ctx.extra, dict) else {}
        expected_result = extra.get("expected_result")
        expected_sql = extra.get("expected_sql")

        if expected_result is None and not expected_sql:
            # Skip non-blockingly: golden set didn't pin a comparison.
            # Emit ``value=NaN`` (and ``passed=True``) so a gate
            # aggregation that's NaN-aware (``mean``, ``pass_rate``)
            # excludes this row from the denominator instead of
            # silently inflating the pass rate. Gates that simply
            # count passed entries still treat it as a pass — which
            # is the right semantics for "this row didn't pin a
            # comparison so don't fault the candidate."
            return [Score(self.id, self.kind, self.layer,
                          float("nan"), True,
                          {"reason": "no expected_result or expected_sql; skipped",
                           "skipped": True})]

        path = _sqlite_path_from_url(self._system.get("url") or ":memory:")
        try:
            conn = _connect_and_seed(path, self._system.get("schema"))
        except sqlite3.Error as e:
            return [Score(self.id, self.kind, self.layer, 0.0, False,
                          {"reason": "shadow_db_setup_failed",
                           "error":  str(e)[:240]})]
        try:
            try:
                cand_rows = _normalize_rows(conn.execute(candidate).fetchall())
            except sqlite3.Error as e:
                return [Score(self.id, self.kind, self.layer, 0.0, False,
                              {"reason": "candidate_execution_error",
                               "error":  str(e)[:240]})]

            if expected_result is not None:
                if not isinstance(expected_result, (list, tuple)):
                    return [Score(self.id, self.kind, self.layer, 0.0, False,
                                  {"reason": "expected_result_not_a_list",
                                   "got_type": type(expected_result).__name__})]
                exp_rows = _normalize_rows(list(expected_result))
                exp_source = "literal"
            else:
                try:
                    exp_rows = _normalize_rows(conn.execute(expected_sql).fetchall())
                    exp_source = "expected_sql"
                except sqlite3.Error as e:
                    return [Score(self.id, self.kind, self.layer, 0.0, False,
                                  {"reason": "expected_sql_execution_error",
                                   "error":  str(e)[:240]})]

            passed, reason = self._compare(cand_rows, exp_rows)
            return [Score(self.id, self.kind, self.layer,
                          1.0 if passed else 0.0, passed,
                          {"system_kind":      "sqlite",
                           "expected_source":  exp_source,
                           "candidate_n_rows": len(cand_rows),
                           "expected_n_rows":  len(exp_rows),
                           "ordered":          self._ordered,
                           "reason":           reason})]
        finally:
            conn.close()

    def _compare(self, cand: list[tuple], exp: list[tuple]) -> tuple[bool, str]:
        if self._columns_strict and cand and exp and len(cand[0]) != len(exp[0]):
            return False, "column count differs"
        if self._ordered:
            return (cand == exp, "exact_match" if cand == exp else "order_or_value_mismatch")
        # Multiset comparison
        if Counter(cand) == Counter(exp):
            return True, "multiset_match"
        return False, "multiset_mismatch"
