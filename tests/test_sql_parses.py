"""``heuristic.sql_parses`` — sqlglot-backed Layer-1 SQL syntactic check.

Skipped wholesale when ``sqlglot`` isn't installed (it's an optional
extra: ``pip install 'evalguard-evaluators[textsql]'``).
"""

from __future__ import annotations

import asyncio

import pytest

sqlglot = pytest.importorskip("sqlglot")  # noqa: F841 — module-level skip

from evalguard_evaluators.base import EvalContext  # noqa: E402
from evalguard_evaluators.heuristics.sql_parses import SqlParsesHeuristic  # noqa: E402


def _ctx(sql: str) -> EvalContext:
    return EvalContext(
        row_id="r", input="q", expected=None, output=sql,
        provider="mock", model="m",
    )


def _run(h: SqlParsesHeuristic, sql: str):
    return asyncio.run(h.evaluate(_ctx(sql)))[0]


def test_valid_select_passes():
    h = SqlParsesHeuristic(); h.configure({})
    s = _run(h, "SELECT id, name FROM users WHERE id = 1;")
    assert s.passed
    assert s.value == 1.0
    assert s.raw["root_kind"] == "Select"


def test_invalid_sql_fails():
    h = SqlParsesHeuristic(); h.configure({})
    # sqlglot is famously permissive (it'll happily reinterpret unknown
    # keywords as identifiers / aliases). Use unmistakably broken SQL.
    s = _run(h, "SELECT FROM WHERE")
    assert not s.passed
    assert s.value == 0.0
    assert s.raw["reason"] == "parse_error"


def test_empty_output_fails():
    h = SqlParsesHeuristic(); h.configure({})
    s = _run(h, "")
    assert not s.passed
    assert s.raw["reason"] == "empty output"


def test_strip_fences_accepts_markdown_block():
    h = SqlParsesHeuristic(); h.configure({"strip_fences": True})
    s = _run(h, "```sql\nSELECT 1;\n```")
    assert s.passed


def test_require_select_rejects_non_select():
    h = SqlParsesHeuristic(); h.configure({"require_select": True})
    s = _run(h, "INSERT INTO t VALUES (1);")
    assert not s.passed
    assert s.raw["reason"] == "root is not SELECT"


def test_dialect_passes_through_to_parser():
    h = SqlParsesHeuristic(); h.configure({"dialect": "postgres"})
    s = _run(h, "SELECT now() AT TIME ZONE 'UTC';")
    assert s.passed
    assert s.raw["dialect"] == "postgres"
