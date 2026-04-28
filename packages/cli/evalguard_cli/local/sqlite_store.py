"""SQLite store mirroring the server's run/score schema (minus tenancy).

Status semantics:
    runs.row_status   "passed" | "failed" | "error" | "cost_capped" — set by the executor.
    runs.gate_status  "passed" | "failed" | "warned" | "none" — set by the gate evaluator.
    runs.status       overall outcome computed at ``finalize_run`` time.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id          TEXT PRIMARY KEY,
  project         TEXT NOT NULL,
  config_hash     TEXT NOT NULL,
  status          TEXT,
  row_status      TEXT,
  gate_status     TEXT,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  cost_usd        REAL DEFAULT 0,
  row_count       INTEGER DEFAULT 0,
  row_pass_count  INTEGER DEFAULT 0,
  row_fail_count  INTEGER DEFAULT 0,
  assets_json     TEXT
);

CREATE TABLE IF NOT EXISTS run_rows (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  row_id          TEXT NOT NULL,
  input_json      TEXT,
  expected_json   TEXT,
  output          TEXT,
  provider        TEXT,
  model           TEXT,
  cost_usd        REAL DEFAULT 0,
  latency_ms      INTEGER DEFAULT 0,
  cache_hit       INTEGER DEFAULT 0,
  tags_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_rows_run ON run_rows(run_id);

CREATE TABLE IF NOT EXISTS scores (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL,
  row_id          TEXT NOT NULL,
  evaluator_id    TEXT NOT NULL,
  evaluator_kind  TEXT NOT NULL,
  layer           INTEGER NOT NULL,
  value           REAL,
  passed          INTEGER,
  raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_run ON scores(run_id);
CREATE INDEX IF NOT EXISTS idx_scores_eval ON scores(run_id, evaluator_id);
CREATE INDEX IF NOT EXISTS idx_scores_layer ON scores(run_id, layer);

CREATE TABLE IF NOT EXISTS gate_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  gate_name     TEXT NOT NULL,
  blocking      INTEGER NOT NULL,
  passed        INTEGER NOT NULL,
  severity      TEXT,
  layer         INTEGER,
  details_json  TEXT
);

CREATE TABLE IF NOT EXISTS assets (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  kind          TEXT NOT NULL,
  asset_id      TEXT NOT NULL,
  version_id    TEXT NOT NULL,
  source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_run ON assets(run_id);
"""


# Idempotent migrations for existing local.db files. Each entry is
# (table, column, ddl-fragment); attempted in order, ignoring duplicates.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("runs",         "row_status",     "TEXT"),
    ("runs",         "gate_status",    "TEXT"),
    ("runs",         "row_pass_count", "INTEGER DEFAULT 0"),
    ("runs",         "row_fail_count", "INTEGER DEFAULT 0"),
    ("runs",         "assets_json",    "TEXT"),
    ("run_rows",     "tags_json",      "TEXT"),
    ("gate_results", "severity",       "TEXT"),
    ("gate_results", "layer",          "INTEGER"),
]


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)
            for table, col, ddl in _MIGRATIONS:
                cols = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # --- run lifecycle -----------------------------------------------------

    def start_run(self, run_id: str, project: str, config_hash: str, assets: list[Any] | None = None) -> None:
        assets_json = json.dumps([_asset_to_dict(a) for a in (assets or [])]) if assets else None
        with self._conn() as c:
            c.execute(
                """INSERT INTO runs(run_id, project, config_hash, started_at, status, assets_json)
                   VALUES (?,?,?,?,?,?)""",
                (run_id, project, config_hash, _now_iso(), "running", assets_json),
            )
            if assets:
                c.executemany(
                    "INSERT INTO assets(run_id,kind,asset_id,version_id,source) VALUES (?,?,?,?,?)",
                    [(run_id, a.kind, a.asset_id, a.version_id, a.source) for a in assets],
                )

    def record_row_results(
        self,
        run_id: str,
        *,
        row_status: str,
        cost_usd: float,
        row_count: int,
        row_pass_count: int,
        row_fail_count: int,
    ) -> None:
        """Called by the executor when row processing finishes."""
        with self._conn() as c:
            c.execute(
                """UPDATE runs SET row_status=?, cost_usd=?, row_count=?,
                                  row_pass_count=?, row_fail_count=?
                   WHERE run_id=?""",
                (row_status, cost_usd, row_count, row_pass_count, row_fail_count, run_id),
            )

    def finalize_run(self, run_id: str, *, status: str, gate_status: str) -> None:
        """Called by the CLI after gate evaluation; sets the final outcome."""
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status=?, gate_status=?, finished_at=? WHERE run_id=?",
                (status, gate_status, _now_iso(), run_id),
            )

    # --- per-row writers ---------------------------------------------------

    def insert_row(
        self,
        run_id: str,
        row_id: str,
        *,
        input_json: Any,
        expected_json: Any,
        output: str,
        provider: str,
        model: str,
        cost_usd: float,
        latency_ms: int,
        cache_hit: bool,
        tags: list[str] | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO run_rows(
                       run_id,row_id,input_json,expected_json,output,provider,model,
                       cost_usd,latency_ms,cache_hit,tags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    row_id,
                    json.dumps(input_json, default=str),
                    json.dumps(expected_json, default=str),
                    output,
                    provider,
                    model,
                    cost_usd,
                    latency_ms,
                    int(cache_hit),
                    json.dumps(tags or []),
                ),
            )

    def insert_scores(self, run_id: str, row_id: str, scores: list[dict[str, Any]]) -> None:
        if not scores:
            return
        with self._conn() as c:
            c.executemany(
                """INSERT INTO scores(run_id,row_id,evaluator_id,evaluator_kind,layer,value,passed,raw_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id,
                        row_id,
                        s["evaluator_id"],
                        s["evaluator_kind"],
                        s["layer"],
                        s["value"],
                        int(bool(s["passed"])),
                        json.dumps(s.get("raw") or {}, default=str),
                    )
                    for s in scores
                ],
            )

    def save_gate_results(self, run_id: str, results: list[Any]) -> None:
        with self._conn() as c:
            c.executemany(
                """INSERT INTO gate_results(
                       run_id,gate_name,blocking,passed,severity,layer,details_json)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, g.name, int(g.blocking), int(g.passed),
                        getattr(g, "severity", "block"),
                        getattr(g, "layer", None),
                        json.dumps(g.details),
                    )
                    for g in results
                ],
            )

    # --- metrics -----------------------------------------------------------

    def compute_metrics(self, run_id: str) -> dict[str, Any]:
        """Build a structured metrics dict consumable by gate rules.

        Returns a flat surface (back-compat with global-gate rules) plus
        ``by_evaluator`` and ``by_layer`` sub-dicts so per-layer gates can
        target a layer or a specific evaluator family.
        """
        with self._conn() as c:
            row_total = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost FROM run_rows WHERE run_id=?",
                (run_id,),
            ).fetchone()
            n_rows = int(row_total["n"] or 0)
            cost = float(row_total["cost"] or 0.0)

            pass_rate = 0.0
            if n_rows:
                failed_rows = int(
                    c.execute(
                        "SELECT COUNT(DISTINCT row_id) AS f FROM scores WHERE run_id=? AND passed=0",
                        (run_id,),
                    ).fetchone()["f"] or 0
                )
                pass_rate = (n_rows - failed_rows) / n_rows

            by_evaluator: dict[str, dict[str, float]] = {}
            evaluator_layer: dict[str, int] = {}
            for r in c.execute(
                """SELECT evaluator_id, evaluator_kind, layer,
                          AVG(value)  AS mean,
                          AVG(passed) AS pass_rate,
                          COUNT(*)    AS n,
                          SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) AS fail_count
                   FROM scores WHERE run_id=? GROUP BY evaluator_id""",
                (run_id,),
            ):
                ev = r["evaluator_id"]
                evaluator_layer[ev] = int(r["layer"])
                by_evaluator[ev] = {
                    "kind": r["evaluator_kind"],
                    "layer": int(r["layer"]),
                    "mean": float(r["mean"] or 0.0),
                    "pass_rate": float(r["pass_rate"] or 0.0),
                    "n": int(r["n"]),
                    "fail_count": int(r["fail_count"] or 0),
                }

            # Per-layer roll-up: pass_rate = fraction of rows where every
            # evaluator at that layer passed.
            by_layer: dict[int, dict[str, Any]] = {}
            for r in c.execute(
                """SELECT layer,
                          AVG(value)  AS mean,
                          AVG(passed) AS pass_rate,
                          COUNT(*)    AS n
                   FROM scores WHERE run_id=? GROUP BY layer""",
                (run_id,),
            ):
                layer = int(r["layer"])
                # rows that ran this layer (denominator) and rows where every
                # score at this layer passed (numerator). Short-circuited
                # rows simply don't appear in either set.
                rows_in_layer = int(c.execute(
                    "SELECT COUNT(DISTINCT row_id) AS n FROM scores WHERE run_id=? AND layer=?",
                    (run_id, layer),
                ).fetchone()["n"] or 0)
                row_pass = int(c.execute(
                    """SELECT COUNT(DISTINCT row_id) AS n_pass FROM scores
                       WHERE run_id=? AND layer=?
                       AND row_id NOT IN (
                         SELECT row_id FROM scores WHERE run_id=? AND layer=? AND passed=0
                       )""",
                    (run_id, layer, run_id, layer),
                ).fetchone()["n_pass"] or 0)
                evaluators = sorted(e for e, l in evaluator_layer.items() if l == layer)
                by_layer[layer] = {
                    "mean": float(r["mean"] or 0.0),
                    "pass_rate": float(r["pass_rate"] or 0.0),
                    "row_pass_rate": (row_pass / rows_in_layer) if rows_in_layer else 0.0,
                    "rows_evaluated": rows_in_layer,
                    "n": int(r["n"]),
                    "evaluators": evaluators,
                }

            # pass_rate_by_tag — built by joining scores to run_rows.tags_json
            by_tag: dict[str, dict[str, Any]] = {}
            tagged_rows = c.execute(
                "SELECT row_id, tags_json FROM run_rows WHERE run_id=?",
                (run_id,),
            ).fetchall()
            row_tags = {r["row_id"]: json.loads(r["tags_json"] or "[]") for r in tagged_rows}
            for row_id, tags in row_tags.items():
                row_failed = bool(
                    c.execute(
                        "SELECT 1 FROM scores WHERE run_id=? AND row_id=? AND passed=0 LIMIT 1",
                        (run_id, row_id),
                    ).fetchone()
                )
                for tag in tags:
                    bucket = by_tag.setdefault(tag, {"n": 0, "passed": 0})
                    bucket["n"] += 1
                    if not row_failed:
                        bucket["passed"] += 1
            for tag, agg in by_tag.items():
                agg["pass_rate"] = (agg["passed"] / agg["n"]) if agg["n"] else 0.0

        # Flat keys for back-compat with the global-gate rule shape
        flat: dict[str, Any] = {
            "row_count": float(n_rows),
            "cost_usd": cost,
            "pass_rate": pass_rate,
        }
        for ev, agg in by_evaluator.items():
            flat[f"{ev}.mean"] = agg["mean"]
            flat[f"{ev}.pass_rate"] = agg["pass_rate"]
        for layer, agg in by_layer.items():
            flat[f"layer{layer}.pass_rate"] = agg["pass_rate"]
            flat[f"layer{layer}.row_pass_rate"] = agg["row_pass_rate"]
            flat[f"layer{layer}.mean"] = agg["mean"]

        flat["by_evaluator"] = by_evaluator
        flat["by_layer"] = by_layer
        flat["by_tag"] = by_tag
        return flat

    # --- read helpers ------------------------------------------------------

    def list_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_gate_results(self, run_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT gate_name, blocking, passed, severity, layer, details_json
                   FROM gate_results WHERE run_id=?""",
                (run_id,),
            ).fetchall()
        return [
            {
                "gate_name": r["gate_name"],
                "blocking": bool(r["blocking"]),
                "passed": bool(r["passed"]),
                "severity": r["severity"] or ("block" if r["blocking"] else "warn"),
                "layer": r["layer"],
                "details": json.loads(r["details_json"] or "[]"),
            }
            for r in rows
        ]


def _asset_to_dict(a: Any) -> dict[str, Any]:
    return {"kind": a.kind, "asset_id": a.asset_id, "version_id": a.version_id, "source": a.source}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
