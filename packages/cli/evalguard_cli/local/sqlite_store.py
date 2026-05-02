"""SQLite store mirroring the server's run/score schema (minus tenancy).

Hierarchy (each level addressable on its own ID, mirrors the future API
shape so the JSON contract stays stable from CLI to server to UI):

    run
     └─ trial          one (provider × prompt) execution
         └─ run_row    one dataset row evaluated by that trial
             └─ score  one evaluator output for that row

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

CREATE TABLE IF NOT EXISTS trials (
  trial_id          TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  provider_id       TEXT NOT NULL,
  provider          TEXT NOT NULL,
  model             TEXT NOT NULL,
  prompt_id         TEXT,
  prompt_version_id TEXT,
  config_json       TEXT,
  row_count         INTEGER DEFAULT 0,
  row_pass_count    INTEGER DEFAULT 0,
  row_fail_count    INTEGER DEFAULT 0,
  cost_usd          REAL DEFAULT 0,
  status            TEXT,
  gate_status       TEXT,
  started_at        TEXT NOT NULL,
  finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(run_id);

CREATE TABLE IF NOT EXISTS run_rows (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  trial_id        TEXT,
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
CREATE INDEX IF NOT EXISTS idx_run_rows_trial ON run_rows(trial_id);

CREATE TABLE IF NOT EXISTS scores (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL,
  trial_id        TEXT,
  row_id          TEXT NOT NULL,
  evaluator_id    TEXT NOT NULL,
  evaluator_kind  TEXT NOT NULL,
  layer           INTEGER NOT NULL,
  value           REAL,
  passed          INTEGER,
  raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_run ON scores(run_id);
CREATE INDEX IF NOT EXISTS idx_scores_trial ON scores(trial_id);
CREATE INDEX IF NOT EXISTS idx_scores_eval ON scores(run_id, evaluator_id);
CREATE INDEX IF NOT EXISTS idx_scores_layer ON scores(run_id, layer);

CREATE TABLE IF NOT EXISTS gate_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  trial_id      TEXT,
  gate_name     TEXT NOT NULL,
  blocking      INTEGER NOT NULL,
  passed        INTEGER NOT NULL,
  severity      TEXT,
  layer         INTEGER,
  details_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_gate_trial ON gate_results(trial_id);

CREATE TABLE IF NOT EXISTS assets (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  kind          TEXT NOT NULL,
  asset_id      TEXT NOT NULL,
  version_id    TEXT NOT NULL,
  source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_run ON assets(run_id);

-- Append-only audit / provenance log. Source of truth for "who/what/when/why";
-- the runs/trials/scores/gate_results tables above are queryable views built
-- by the executor as it emits events. ``prev_event_hash`` chains events per
-- ``run_id`` partition (single-writer hash chain — sufficient for self-host).
CREATE TABLE IF NOT EXISTS events (
  event_id        TEXT PRIMARY KEY,        -- ULID, sortable
  kind            TEXT NOT NULL,           -- 'run.started', 'evaluator.judge.invoked', ...
  run_id          TEXT NOT NULL,
  trial_id        TEXT,
  row_id          TEXT,
  actor_id        TEXT NOT NULL,
  actor_type      TEXT NOT NULL,           -- cli | ci | api_key | system
  actor_meta_json TEXT,
  subject_kind    TEXT,
  subject_id      TEXT,
  subject_version TEXT,                    -- content-hashed asset version_id
  inputs_hash     TEXT,                    -- sha256 of canonical inputs
  outputs_hash    TEXT,                    -- sha256 of canonical outputs
  payload_json    TEXT,
  cost_usd        REAL,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  duration_ms     INTEGER,
  trace_id        TEXT,                    -- per-run W3C trace id
  span_id         TEXT,
  parent_span_id  TEXT,
  prev_event_hash TEXT,
  event_hash      TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_events_trial ON events(trial_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(run_id, kind);
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
    ("run_rows",     "trial_id",       "TEXT"),
    ("scores",       "trial_id",       "TEXT"),
    ("gate_results", "severity",       "TEXT"),
    ("gate_results", "layer",          "INTEGER"),
    ("gate_results", "trial_id",       "TEXT"),
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

    # --- trial lifecycle ---------------------------------------------------

    def start_trial(
        self,
        trial_id: str,
        run_id: str,
        *,
        provider_id: str,
        provider: str,
        model: str,
        prompt_id: str | None,
        prompt_version_id: str | None,
        config: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO trials(
                       trial_id, run_id, provider_id, provider, model,
                       prompt_id, prompt_version_id, config_json,
                       started_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    trial_id, run_id, provider_id, provider, model,
                    prompt_id, prompt_version_id,
                    json.dumps(config or {}, default=str),
                    _now_iso(),
                    "running",
                ),
            )

    def record_trial_results(
        self,
        trial_id: str,
        *,
        row_status: str,
        cost_usd: float,
        row_count: int,
        row_pass_count: int,
        row_fail_count: int,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE trials SET status=?, cost_usd=?, row_count=?,
                                    row_pass_count=?, row_fail_count=?
                   WHERE trial_id=?""",
                (row_status, cost_usd, row_count, row_pass_count, row_fail_count, trial_id),
            )

    def finalize_trial(self, trial_id: str, *, status: str, gate_status: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE trials SET status=?, gate_status=?, finished_at=? WHERE trial_id=?",
                (status, gate_status, _now_iso(), trial_id),
            )

    def list_trials(self, run_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trials WHERE run_id=? ORDER BY started_at",
                (run_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "trial_id":          r["trial_id"],
                "run_id":            r["run_id"],
                "provider_id":       r["provider_id"],
                "provider":          r["provider"],
                "model":             r["model"],
                "prompt_id":         r["prompt_id"],
                "prompt_version_id": r["prompt_version_id"],
                "config":            json.loads(r["config_json"] or "{}"),
                "row_count":         int(r["row_count"] or 0),
                "row_pass_count":    int(r["row_pass_count"] or 0),
                "row_fail_count":    int(r["row_fail_count"] or 0),
                "cost_usd":          float(r["cost_usd"] or 0.0),
                "status":            r["status"],
                "gate_status":       r["gate_status"],
                "started_at":        r["started_at"],
                "finished_at":       r["finished_at"],
            })
        return out

    # --- per-row writers ---------------------------------------------------

    def insert_row(
        self,
        run_id: str,
        row_id: str,
        *,
        trial_id: str | None = None,
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
                       run_id,trial_id,row_id,input_json,expected_json,output,provider,model,
                       cost_usd,latency_ms,cache_hit,tags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    trial_id,
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

    def insert_scores(
        self,
        run_id: str,
        row_id: str,
        scores: list[dict[str, Any]],
        *,
        trial_id: str | None = None,
    ) -> None:
        if not scores:
            return
        with self._conn() as c:
            c.executemany(
                """INSERT INTO scores(
                       run_id,trial_id,row_id,evaluator_id,evaluator_kind,layer,value,passed,raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id,
                        trial_id,
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

    def save_gate_results(
        self,
        run_id: str,
        results: list[Any],
        *,
        trial_id: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.executemany(
                """INSERT INTO gate_results(
                       run_id,trial_id,gate_name,blocking,passed,severity,layer,details_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, trial_id, g.name, int(g.blocking), int(g.passed),
                        getattr(g, "severity", "block"),
                        getattr(g, "layer", None),
                        json.dumps(g.details),
                    )
                    for g in results
                ],
            )

    # --- metrics -----------------------------------------------------------

    def compute_metrics(self, run_id: str, *, trial_id: str | None = None) -> dict[str, Any]:
        """Build a structured metrics dict consumable by gate rules.

        With ``trial_id``, scope the metrics to a single trial. Without
        it, aggregate across every trial in the run.

        Returns a flat surface (back-compat with global-gate rules) plus
        ``by_evaluator`` and ``by_layer`` sub-dicts so per-layer gates
        can target a layer or a specific evaluator family.
        """
        scope_clause, scope_args = _scope_clause(trial_id)
        with self._conn() as c:
            row_total = c.execute(
                f"SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost "
                f"FROM run_rows WHERE run_id=? {scope_clause}",
                (run_id, *scope_args),
            ).fetchone()
            n_rows = int(row_total["n"] or 0)
            cost = float(row_total["cost"] or 0.0)

            pass_rate = 0.0
            if n_rows:
                failed_rows = int(
                    c.execute(
                        f"""SELECT COUNT(*) AS f FROM (
                              SELECT trial_id, row_id FROM scores
                              WHERE run_id=? {scope_clause} AND passed=0
                              GROUP BY trial_id, row_id
                            )""",
                        (run_id, *scope_args),
                    ).fetchone()["f"] or 0
                )
                pass_rate = (n_rows - failed_rows) / n_rows

            by_evaluator: dict[str, dict[str, float]] = {}
            evaluator_layer: dict[str, int] = {}
            for r in c.execute(
                f"""SELECT evaluator_id, evaluator_kind, layer,
                          AVG(value)  AS mean,
                          AVG(passed) AS pass_rate,
                          COUNT(*)    AS n,
                          SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) AS fail_count
                   FROM scores WHERE run_id=? {scope_clause} GROUP BY evaluator_id""",
                (run_id, *scope_args),
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

            by_layer: dict[int, dict[str, Any]] = {}
            for r in c.execute(
                f"""SELECT layer,
                          AVG(value)  AS mean,
                          AVG(passed) AS pass_rate,
                          COUNT(*)    AS n
                   FROM scores WHERE run_id=? {scope_clause} GROUP BY layer""",
                (run_id, *scope_args),
            ):
                layer = int(r["layer"])
                rows_in_layer = int(c.execute(
                    f"""SELECT COUNT(*) AS n FROM (
                          SELECT trial_id, row_id FROM scores
                          WHERE run_id=? {scope_clause} AND layer=?
                          GROUP BY trial_id, row_id
                        )""",
                    (run_id, *scope_args, layer),
                ).fetchone()["n"] or 0)
                row_pass = int(c.execute(
                    f"""SELECT COUNT(*) AS n_pass FROM (
                          SELECT trial_id, row_id
                          FROM scores
                          WHERE run_id=? {scope_clause} AND layer=?
                          GROUP BY trial_id, row_id
                          HAVING SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) = 0
                        )""",
                    (run_id, *scope_args, layer),
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

            # pass_rate_by_tag
            by_tag: dict[str, dict[str, Any]] = {}
            tagged_rows = c.execute(
                f"SELECT trial_id, row_id, tags_json FROM run_rows WHERE run_id=? {scope_clause}",
                (run_id, *scope_args),
            ).fetchall()
            for r in tagged_rows:
                row_id = r["row_id"]
                row_trial_id = r["trial_id"]
                tags = json.loads(r["tags_json"] or "[]")
                if trial_id is None:
                    row_failed = bool(
                        c.execute(
                            "SELECT 1 FROM scores WHERE run_id=? AND trial_id=? "
                            "AND row_id=? AND passed=0 LIMIT 1",
                            (run_id, row_trial_id, row_id),
                        ).fetchone()
                    )
                else:
                    row_failed = bool(
                        c.execute(
                            f"SELECT 1 FROM scores WHERE run_id=? {scope_clause} "
                            f"AND row_id=? AND passed=0 LIMIT 1",
                            (run_id, *scope_args, row_id),
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

    # --- audit events ------------------------------------------------------

    def insert_event(self, event: dict[str, Any]) -> None:
        """Append one fully-formed event row. ``event_hash`` must be set."""
        with self._conn() as c:
            c.execute(
                """INSERT INTO events(
                       event_id, kind, run_id, trial_id, row_id,
                       actor_id, actor_type, actor_meta_json,
                       subject_kind, subject_id, subject_version,
                       inputs_hash, outputs_hash, payload_json,
                       cost_usd, started_at, finished_at, duration_ms,
                       trace_id, span_id, parent_span_id,
                       prev_event_hash, event_hash)
                   VALUES (?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?,?, ?,?,?, ?,?)""",
                (
                    event["event_id"], event["kind"], event["run_id"],
                    event.get("trial_id"), event.get("row_id"),
                    event["actor_id"], event["actor_type"],
                    json.dumps(event.get("actor_meta") or {}, default=str),
                    event.get("subject_kind"), event.get("subject_id"),
                    event.get("subject_version"),
                    event.get("inputs_hash"), event.get("outputs_hash"),
                    json.dumps(event.get("payload") or {}, default=str),
                    event.get("cost_usd"),
                    event["started_at"], event.get("finished_at"),
                    event.get("duration_ms"),
                    event.get("trace_id"), event.get("span_id"),
                    event.get("parent_span_id"),
                    event.get("prev_event_hash"), event["event_hash"],
                ),
            )

    def last_event_hash(self, run_id: str) -> str | None:
        """Most recent ``event_hash`` for the run (the chain tip).

        Ordered by ``rowid`` (insert order) so two events emitted in
        the same millisecond can't be reordered by ULID-tail randomness
        — that would surface as a false ``verify_chain`` failure.
        """
        with self._conn() as c:
            r = c.execute(
                "SELECT event_hash FROM events WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return r["event_hash"] if r else None

    def list_events(
        self,
        run_id: str,
        *,
        kind: str | None = None,
        trial_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["run_id=?"]
        args: list[Any] = [run_id]
        if kind is not None:
            clauses.append("kind=?")
            args.append(kind)
        if trial_id is not None:
            clauses.append("trial_id=?")
            args.append(trial_id)
        # ``ORDER BY rowid`` preserves insert order across ULID-tail
        # collisions within the same millisecond. Required for chain
        # verification — see ``last_event_hash`` for the rationale.
        sql = (
            "SELECT * FROM events WHERE " + " AND ".join(clauses)
            + " ORDER BY rowid"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [_event_row_to_dict(r) for r in rows]

    def list_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_rows(self, run_id: str, *, trial_id: str | None = None) -> list[dict[str, Any]]:
        """One entry per row, with aggregated pass/fail across scores."""
        scope = "AND r.trial_id=?" if trial_id else ""
        scope_score = "AND s.trial_id=?" if trial_id else ""
        # Argument order matches placeholder order in the SQL below:
        # scope_score → r.run_id → scope.
        if trial_id:
            args: tuple[Any, ...] = (trial_id, run_id, trial_id)
        else:
            args = (run_id,)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT r.row_id, r.trial_id, r.provider, r.model, r.cost_usd,
                          r.latency_ms, r.cache_hit, r.tags_json,
                          COUNT(s.id) AS n_scores,
                          SUM(CASE WHEN s.passed=0 THEN 1 ELSE 0 END) AS n_failed
                   FROM run_rows r LEFT JOIN scores s
                     ON s.run_id = r.run_id AND s.row_id = r.row_id
                        {scope_score}
                   WHERE r.run_id = ? {scope}
                   GROUP BY r.id
                   ORDER BY r.id""",
                args,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "row_id":    r["row_id"],
                "trial_id":  r["trial_id"],
                "provider":  r["provider"],
                "model":     r["model"],
                "cost_usd":  float(r["cost_usd"] or 0.0),
                "latency_ms": int(r["latency_ms"] or 0),
                "cache_hit": bool(r["cache_hit"]),
                "tags":      json.loads(r["tags_json"] or "[]"),
                "n_scores":  int(r["n_scores"] or 0),
                "passed":    int(r["n_failed"] or 0) == 0,
            })
        return out

    def get_row(
        self, run_id: str, row_id: str, *, trial_id: str | None = None,
    ) -> dict[str, Any] | None:
        row_scope = "AND trial_id=?" if trial_id else ""
        score_scope = "AND trial_id=?" if trial_id else ""
        args: tuple[Any, ...] = (trial_id,) if trial_id else ()
        with self._conn() as c:
            r = c.execute(
                f"SELECT * FROM run_rows WHERE run_id=? AND row_id=? {row_scope} LIMIT 1",
                (run_id, row_id, *args),
            ).fetchone()
            if not r:
                return None
            scores = c.execute(
                f"""SELECT evaluator_id, evaluator_kind, layer, value, passed, raw_json
                    FROM scores WHERE run_id=? AND row_id=? {score_scope}
                    ORDER BY layer, evaluator_id""",
                (run_id, row_id, *args),
            ).fetchall()
        return {
            "row_id":     r["row_id"],
            "trial_id":   r["trial_id"],
            "input":      json.loads(r["input_json"] or "null"),
            "expected":   json.loads(r["expected_json"] or "null"),
            "output":     r["output"],
            "provider":   r["provider"],
            "model":      r["model"],
            "cost_usd":   float(r["cost_usd"] or 0.0),
            "latency_ms": int(r["latency_ms"] or 0),
            "cache_hit":  bool(r["cache_hit"]),
            "tags":       json.loads(r["tags_json"] or "[]"),
            "scores": [
                {
                    "evaluator_id":   s["evaluator_id"],
                    "evaluator_kind": s["evaluator_kind"],
                    "layer":          int(s["layer"]),
                    "value":          float(s["value"] or 0.0),
                    "passed":         bool(s["passed"]),
                    "raw":            json.loads(s["raw_json"] or "null"),
                }
                for s in scores
            ],
        }

    def get_gate_results(
        self, run_id: str, *, trial_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scope = " AND trial_id=?" if trial_id else " AND trial_id IS NULL"
        args: tuple[Any, ...] = (trial_id,) if trial_id else ()
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT gate_name, blocking, passed, severity, layer, details_json, trial_id
                    FROM gate_results WHERE run_id=? {scope}""",
                (run_id, *args),
            ).fetchall()
        return [
            {
                "gate_name": r["gate_name"],
                "trial_id":  r["trial_id"],
                "blocking":  bool(r["blocking"]),
                "passed":    bool(r["passed"]),
                "severity":  r["severity"] or ("block" if r["blocking"] else "warn"),
                "layer":     r["layer"],
                "details":   json.loads(r["details_json"] or "[]"),
            }
            for r in rows
        ]

    def compute_comparison(self, run_id: str) -> dict[str, Any]:
        """Cross-trial winner table for the most engineering-relevant scalars.

        Emits ``best_by`` keyed by metric name; each value records which
        trial wins (and the runner-up for context). 'Best' means *higher*
        for quality-style metrics and *lower* for ``cost_usd`` / ``latency``.
        """
        trials = self.list_trials(run_id)
        if len(trials) < 2:
            return {"best_by": {}, "trials": [t["trial_id"] for t in trials]}

        per_trial = {t["trial_id"]: self.compute_metrics(run_id, trial_id=t["trial_id"]) for t in trials}

        # Pick a small set of scalars worth comparing. Anything with
        # ``cost`` or ``latency`` in the name is lower-better.
        candidate_keys: set[str] = set()
        for m in per_trial.values():
            for k, v in m.items():
                if isinstance(v, (int, float)) and k not in {"row_count"}:
                    candidate_keys.add(k)
        # cost shows up at the trial level, not in metrics — surface explicitly.
        candidate_keys.update({"cost_usd", "row_pass_count"})

        best_by: dict[str, dict[str, Any]] = {}
        for key in sorted(candidate_keys):
            lower_better = any(s in key for s in ("cost", "latency"))
            scored: list[tuple[str, float]] = []
            for t in trials:
                m = per_trial[t["trial_id"]]
                if key in m and isinstance(m[key], (int, float)):
                    val = float(m[key])
                elif key == "cost_usd":
                    val = float(t["cost_usd"])
                elif key == "row_pass_count":
                    val = float(t["row_pass_count"])
                else:
                    continue
                scored.append((t["trial_id"], val))
            if len(scored) < 2:
                continue
            scored.sort(key=lambda x: x[1], reverse=not lower_better)
            winner_id, winner_val = scored[0]
            runner_id, runner_val = scored[1]
            winner = next(t for t in trials if t["trial_id"] == winner_id)
            runner = next(t for t in trials if t["trial_id"] == runner_id)
            best_by[key] = {
                "winner": {
                    "trial_id":    winner_id,
                    "provider_id": winner["provider_id"],
                    "value":       winner_val,
                },
                "runner_up": {
                    "trial_id":    runner_id,
                    "provider_id": runner["provider_id"],
                    "value":       runner_val,
                },
                "lower_better": lower_better,
            }
        return {
            "best_by": best_by,
            "trials":  [t["trial_id"] for t in trials],
        }


def _event_row_to_dict(r: Any) -> dict[str, Any]:
    """Lossless round-trip: a NULL column comes back as ``None``, never 0/0.0.

    The hash chain depends on this — ``_hash_event`` will recompute over
    the read-back dict, and any normalization (e.g. ``None`` → ``0.0``)
    silently invalidates every event past it.
    """
    return {
        "event_id":        r["event_id"],
        "kind":            r["kind"],
        "run_id":          r["run_id"],
        "trial_id":        r["trial_id"],
        "row_id":          r["row_id"],
        "actor_id":        r["actor_id"],
        "actor_type":      r["actor_type"],
        "actor_meta":      json.loads(r["actor_meta_json"]) if r["actor_meta_json"] else {},
        "subject_kind":    r["subject_kind"],
        "subject_id":      r["subject_id"],
        "subject_version": r["subject_version"],
        "inputs_hash":     r["inputs_hash"],
        "outputs_hash":    r["outputs_hash"],
        "payload":         json.loads(r["payload_json"]) if r["payload_json"] else {},
        "cost_usd":        float(r["cost_usd"]) if r["cost_usd"] is not None else None,
        "started_at":      r["started_at"],
        "finished_at":     r["finished_at"],
        "duration_ms":     int(r["duration_ms"]) if r["duration_ms"] is not None else None,
        "trace_id":        r["trace_id"],
        "span_id":         r["span_id"],
        "parent_span_id":  r["parent_span_id"],
        "prev_event_hash": r["prev_event_hash"],
        "event_hash":      r["event_hash"],
    }


def _asset_to_dict(a: Any) -> dict[str, Any]:
    return {"kind": a.kind, "asset_id": a.asset_id, "version_id": a.version_id, "source": a.source}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scope_clause(trial_id: str | None) -> tuple[str, tuple[Any, ...]]:
    """Optional ``AND trial_id=?`` predicate (or empty when aggregating)."""
    if trial_id is None:
        return "", ()
    return "AND trial_id=?", (trial_id,)
