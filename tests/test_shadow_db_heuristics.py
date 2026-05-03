"""Layer-1 shadow-DB heuristics: dry_run_on_shadow_db + result_set_equivalence.

Covers:
- happy path: SELECT executes against a seeded SQLite shadow → pass.
- candidate that references a missing table → execution_error → fail.
- result-set equivalence: candidate matches expected_sql under the
  default multiset comparison, including when row order differs.
- result-set equivalence: ``expected_result`` (literal) wins over
  expected_sql when both are present.
- result-set equivalence: missing both expected_sql and
  expected_result → non-blocking pass with reason.
- end-to-end: a yaml-loaded config inlines the system into the
  evaluator spec under ``_system`` so version_id covers the binding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from evalguard_evaluators.base import EvalContext
from evalguard_evaluators.heuristics.dry_run_on_shadow_db import (
    DryRunOnShadowDbHeuristic,
)
from evalguard_evaluators.heuristics.result_set_equivalence import (
    ResultSetEquivalenceHeuristic,
)

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


# A self-contained DDL+seed for the in-test shadow DB.
_SCHEMA_SQL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, country TEXT NOT NULL);
INSERT INTO customers (id, country) VALUES (1,'US'),(2,'US'),(3,'DE');
"""


def _system(schema: str = _SCHEMA_SQL) -> dict:
    return {"name": "shadow", "kind": "sqlite", "url": ":memory:", "schema": schema}


def _ctx(output: str, *, expected_sql: str | None = None,
         expected_result=None) -> EvalContext:
    extra: dict = {}
    if expected_sql is not None:
        extra["expected_sql"] = expected_sql
    if expected_result is not None:
        extra["expected_result"] = expected_result
    return EvalContext(
        row_id="r", input="q", expected=None, output=output,
        provider="mock", model="m", extra=extra,
    )


def _run(ev, ctx):
    return asyncio.run(ev.evaluate(ctx))[0]


# ---------------------------------------------------------------------------
# dry_run_on_shadow_db


def test_dry_run_passes_on_valid_select():
    h = DryRunOnShadowDbHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx("SELECT id FROM customers WHERE country = 'US';"))
    assert s.passed
    assert s.value == 1.0
    assert s.raw["system_kind"] == "sqlite"


def test_dry_run_fails_on_missing_table():
    h = DryRunOnShadowDbHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx("SELECT * FROM nope;"))
    assert not s.passed
    assert s.raw["reason"] == "execution_error"
    assert "no such table" in s.raw["error"].lower()


def test_dry_run_strips_markdown_fence():
    h = DryRunOnShadowDbHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx("```sql\nSELECT 1;\n```"))
    assert s.passed


def test_dry_run_fails_on_empty_output():
    h = DryRunOnShadowDbHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx(""))
    assert not s.passed
    assert s.raw["reason"] == "empty output"


# ---------------------------------------------------------------------------
# result_set_equivalence


def test_result_set_equivalence_passes_on_matching_query():
    h = ResultSetEquivalenceHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx(
        "SELECT country, COUNT(*) FROM customers GROUP BY country;",
        expected_sql="SELECT country, COUNT(*) FROM customers GROUP BY country;",
    ))
    assert s.passed
    assert s.raw["expected_source"] == "expected_sql"


def test_result_set_equivalence_passes_when_only_order_differs_and_unordered():
    h = ResultSetEquivalenceHeuristic()
    h.configure({"_system": _system(), "ordered": False})
    s = _run(h, _ctx(
        "SELECT country, COUNT(*) FROM customers GROUP BY country ORDER BY country DESC;",
        expected_sql="SELECT country, COUNT(*) FROM customers GROUP BY country ORDER BY country ASC;",
    ))
    assert s.passed
    assert s.raw["reason"] == "multiset_match"


def test_result_set_equivalence_fails_on_wrong_filter():
    h = ResultSetEquivalenceHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx(
        "SELECT id FROM customers WHERE country = 'DE';",  # 1 row
        expected_sql="SELECT id FROM customers WHERE country = 'US';",  # 2 rows
    ))
    assert not s.passed
    assert s.raw["reason"] == "multiset_mismatch"


def test_result_set_equivalence_uses_expected_result_when_present():
    h = ResultSetEquivalenceHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx(
        "SELECT COUNT(*) FROM customers WHERE country = 'US';",
        expected_result=[[2]],   # literal — bypasses expected_sql
        expected_sql="SELECT 999;",  # would mismatch if used
    ))
    assert s.passed
    assert s.raw["expected_source"] == "literal"


def test_result_set_equivalence_skips_when_no_expected():
    h = ResultSetEquivalenceHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx("SELECT id FROM customers;"))
    # Skipped: row didn't pin a comparison. Don't tank the gate.
    assert s.passed
    assert "skipped" in s.raw["reason"]


def test_result_set_equivalence_fails_on_candidate_execution_error():
    h = ResultSetEquivalenceHeuristic()
    h.configure({"_system": _system()})
    s = _run(h, _ctx("SELECT * FROM nope;",
                     expected_sql="SELECT id FROM customers;"))
    assert not s.passed
    assert s.raw["reason"] == "candidate_execution_error"


# ---------------------------------------------------------------------------
# YAML wiring: top-level systems[shadow] inlines into the evaluator spec.


def test_yaml_loader_inlines_system_into_evaluator_spec(tmp_path: Path):
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"q","expected_sql":"SELECT 1;","params":{"output":"SELECT 1;"}}\n'
    )
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "db.sql").write_text("CREATE TABLE t (id INTEGER);")
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: fixed, output: 'SELECT 1;', latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "systems:\n"
        "  shadow:\n"
        "    kind:    sqlite\n"
        "    url:     ':memory:'\n"
        "    schema:  schemas/db.sql\n"
        "heuristics:\n"
        "  - id: dry\n"
        "    type: dry_run_on_shadow_db\n"
        "    system: shadow\n"
    )
    loaded = load_config(cfg_path)
    [heuristic_spec] = loaded.raw["heuristics"]
    # The system was inlined, not just referenced by name.
    assert "_system" in heuristic_spec
    assert heuristic_spec["_system"]["kind"] == "sqlite"
    # Schema file content was inlined too.
    assert heuristic_spec["_system"]["schema"].startswith("CREATE TABLE")
    assert heuristic_spec["_system"]["schema_source"] == "schemas/db.sql"
    # The schema asset is recorded so it shows up in audit lineage.
    assert any(a.kind == "schema" and a.asset_id.endswith("system.shadow")
               for a in loaded.assets)
    # The heuristic version_id covers the inlined system binding.
    assert heuristic_spec["version_id"]


def test_yaml_loader_raises_on_unknown_system_reference(tmp_path: Path):
    import pytest
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text('{"id":"r1","input":"q"}\n')
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics:\n"
        "  - id: dry\n"
        "    type: dry_run_on_shadow_db\n"
        "    system: nope\n"
    )
    with pytest.raises(ValueError, match="references system 'nope'"):
        load_config(cfg_path)


def test_end_to_end_dry_run_via_executor(tmp_path: Path):
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"good","input":"q","params":{"output":"SELECT id FROM t;"}}\n'
        '{"id":"bad","input":"q","params":{"output":"SELECT * FROM nope;"}}\n'
    )
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "db.sql").write_text(
        "CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1),(2);"
    )
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: fixed, output: 'SELECT 1;', latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "systems:\n"
        "  shadow: { kind: sqlite, url: ':memory:', schema: schemas/db.sql }\n"
        "heuristics:\n"
        "  - { id: dry, type: dry_run_on_shadow_db, system: shadow }\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    [trial] = store.list_trials(record.run_id)
    good = store.get_row(record.run_id, "good", trial_id=trial["trial_id"])
    bad  = store.get_row(record.run_id, "bad",  trial_id=trial["trial_id"])
    good_passed = all(s["passed"] for s in good["scores"])
    bad_passed  = all(s["passed"] for s in bad["scores"])
    assert good_passed
    assert not bad_passed
