"""SQLite store mirroring the server's run/score schema (minus tenancy)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id        TEXT PRIMARY KEY,
  project       TEXT NOT NULL,
  config_hash   TEXT NOT NULL,
  status        TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  cost_usd      REAL DEFAULT 0,
  row_count     INTEGER DEFAULT 0
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
  cache_hit       INTEGER DEFAULT 0
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

CREATE TABLE IF NOT EXISTS gate_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  gate_name     TEXT NOT NULL,
  blocking      INTEGER NOT NULL,
  passed        INTEGER NOT NULL,
  details_json  TEXT
);
"""


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

    def start_run(self, run_id: str, project: str, config_hash: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs(run_id, project, config_hash, status, started_at) VALUES (?,?,?,?,?)",
                (run_id, project, config_hash, "running", _now_iso()),
            )

    def finish_run(self, run_id: str, *, status: str, cost_usd: float, row_count: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status=?, finished_at=?, cost_usd=?, row_count=? WHERE run_id=?",
                (status, _now_iso(), cost_usd, row_count, run_id),
            )

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
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO run_rows(
                       run_id,row_id,input_json,expected_json,output,provider,model,
                       cost_usd,latency_ms,cache_hit)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
                """INSERT INTO gate_results(run_id,gate_name,blocking,passed,details_json)
                   VALUES (?,?,?,?,?)""",
                [
                    (run_id, g.name, int(g.blocking), int(g.passed), json.dumps(g.details))
                    for g in results
                ],
            )

    def compute_metrics(self, run_id: str) -> dict[str, float]:
        """Build a flat metrics dict consumable by the gate evaluator."""
        with self._conn() as c:
            row_total = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost FROM run_rows WHERE run_id=?",
                (run_id,),
            ).fetchone()
            n_rows = row_total["n"] or 0
            cost = float(row_total["cost"] or 0.0)

            # pass_rate = fraction of rows where every score on that row passed
            pass_rate = 0.0
            if n_rows:
                failed_rows = c.execute(
                    """SELECT COUNT(DISTINCT row_id) AS f FROM scores
                       WHERE run_id=? AND passed=0""",
                    (run_id,),
                ).fetchone()["f"] or 0
                pass_rate = (n_rows - failed_rows) / n_rows

            # per-evaluator means
            evaluator_means: dict[str, float] = {}
            for r in c.execute(
                """SELECT evaluator_id, AVG(value) AS mean, AVG(passed) AS pass_rate
                   FROM scores WHERE run_id=? GROUP BY evaluator_id""",
                (run_id,),
            ):
                evaluator_means[f"{r['evaluator_id']}.mean"] = float(r["mean"] or 0.0)
                evaluator_means[f"{r['evaluator_id']}.pass_rate"] = float(r["pass_rate"] or 0.0)

        return {
            "row_count": float(n_rows),
            "cost_usd": cost,
            "pass_rate": pass_rate,
            **evaluator_means,
        }

    def list_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
