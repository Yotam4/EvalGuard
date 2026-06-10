"""Direct unit tests for ``evalguard_api.worker.run_async_evaluator``.

These tests invoke the worker's job function with a hand-built
context dict and a hand-built payload — no Arq runtime, no Redis,
no fakeredis.  The whole point of the worker function is that it's
testable in isolation; the integration tests exercise the
enqueue → dequeue path separately.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from evalguard_api.config import Settings
from evalguard_api.db import make_engine
from evalguard_api.live import (
    LiveCallRecord, ensure_live_run, ensure_live_trial, record_call,
)
from evalguard_api.worker import AsyncEvalJob, run_async_evaluator


# ---------------------------------------------------------------------------
# Fixtures: a seeded run_rows row to UPDATE.


@pytest.fixture
def worker_engine(settings: Settings):
    """Re-use the API's settings fixture (per-test SQLite) to build
    an engine the worker code can use.  Tests that exercise the API
    via TestClient also share this DB."""
    engine = make_engine(settings)
    yield engine
    engine.dispose()


@pytest.fixture
def seeded_row(client, auth_headers, worker_engine):
    """Push a config + invoke once so we have a real ``run_rows`` row
    + ``runs`` + ``trials`` aggregates for the worker to UPDATE.

    Returns ``(run_id, trial_id, row_id, row_pk, project_id, org_id)``.
    """
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "heuristics:\n"
        "  - id: len-ok\n"
        "    type: length\n"
        "    max: 10000\n"
    )
    client.post(
        "/v1/projects/default/config", json={"content": cfg},
        headers=auth_headers,
    )
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "seed row"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    with worker_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rr.id, rr.project_id, p.org_id "
                "FROM run_rows rr "
                "JOIN projects p ON p.project_id = rr.project_id "
                "WHERE rr.row_id = :rid"
            ),
            {"rid": body["row_id"]},
        ).mappings().fetchone()
    assert row is not None
    return {
        "run_id":     body["run_id"],
        "trial_id":   body["trial_id"],
        "row_id":     body["row_id"],
        "row_pk":     int(row["id"]),
        "project_id": row["project_id"],
        "org_id":     row["org_id"],
    }


def _job_payload(seeded: dict, *, ep_name: str, ev_cfg: dict) -> dict:
    return AsyncEvalJob(
        project_id=seeded["project_id"], org_id=seeded["org_id"],
        run_id=seeded["run_id"], trial_id=seeded["trial_id"],
        row_id=seeded["row_id"], row_pk=seeded["row_pk"],
        ep_name=ep_name, ev_cfg=ev_cfg,
        eval_context={
            "row_id": seeded["row_id"], "input": "x", "expected": None,
            "output": "result", "provider": "mock", "model": "m", "extra": {},
        },
        actor_key_id="key_test", actor_scopes=["admin"],
    ).as_dict()


# ---------------------------------------------------------------------------
# Happy path


def test_run_async_evaluator_lands_score_on_row(seeded_row, worker_engine):
    """End-to-end of the job function: pass a passing mock judge,
    assert the score lands in ``detail_json.async_scores`` and the
    audit chain records ``evaluator.scored_async``."""
    payload = _job_payload(
        seeded_row,
        ep_name="judge.mock_pointwise",
        ev_cfg={"id": "j1", "score": 5.0, "threshold": 4.0},
    )
    result = asyncio.run(run_async_evaluator({"engine": worker_engine}, payload))
    assert result["ok"] is True
    assert result["score_count"] == 1
    assert result["all_passed"] is True

    with worker_engine.connect() as conn:
        row = conn.execute(
            text("SELECT passed, n_scores, detail_json FROM run_rows WHERE id = :pk"),
            {"pk": seeded_row["row_pk"]},
        ).mappings().fetchone()
    assert row is not None
    detail = json.loads(row["detail_json"])
    async_scores = detail["async_scores"]
    assert "j1" in async_scores
    assert async_scores["j1"]["passed"] is True
    assert async_scores["j1"]["value"] == 5.0
    # passed AND True == passed (unchanged).
    assert bool(row["passed"]) is True
    # n_scores incremented by 1.
    assert row["n_scores"] == 2  # 1 inline (length heuristic) + 1 async


def test_run_async_evaluator_flips_passed_on_failing_score(seeded_row, worker_engine):
    """A failing async score must flip the row's ``passed`` to False
    AND decrement the parent run + trial pass-count aggregates."""
    payload = _job_payload(
        seeded_row,
        ep_name="judge.mock_pointwise",
        ev_cfg={"id": "fail-judge", "score": 1.0, "threshold": 4.0},
    )
    asyncio.run(run_async_evaluator({"engine": worker_engine}, payload))

    with worker_engine.connect() as conn:
        row = conn.execute(
            text("SELECT passed FROM run_rows WHERE id = :pk"),
            {"pk": seeded_row["row_pk"]},
        ).scalar()
        run = conn.execute(
            text("SELECT row_pass_count, row_fail_count FROM runs WHERE run_id = :rid"),
            {"rid": seeded_row["run_id"]},
        ).mappings().fetchone()
        trial = conn.execute(
            text("SELECT row_pass_count, row_fail_count FROM trials WHERE trial_id = :tid"),
            {"tid": seeded_row["trial_id"]},
        ).mappings().fetchone()

    assert bool(row) is False
    # Was 1/0 before the async score; should now be 0/1.
    assert run["row_pass_count"] == 0
    assert run["row_fail_count"] == 1
    assert trial["row_pass_count"] == 0
    assert trial["row_fail_count"] == 1


# ---------------------------------------------------------------------------
# Failure modes


def test_run_async_evaluator_records_failure_on_evaluator_exception(
    seeded_row, worker_engine,
):
    """A broken evaluator (raises during evaluate) must emit
    ``evaluator.failed_async`` without mutating the row."""
    # ``judge.pointwise`` needs a rubric — config without one raises
    # in configure().
    payload = _job_payload(
        seeded_row,
        ep_name="judge.pointwise",
        ev_cfg={"id": "broken"},  # no rubric → ValueError
    )
    result = asyncio.run(run_async_evaluator({"engine": worker_engine}, payload))
    assert result["ok"] is False
    assert result["stage"] == "evaluate"

    with worker_engine.connect() as conn:
        # Row's passed + n_scores unchanged from the seed.
        row = conn.execute(
            text("SELECT passed, n_scores FROM run_rows WHERE id = :pk"),
            {"pk": seeded_row["row_pk"]},
        ).mappings().fetchone()
        # Audit event landed.
        kinds = [
            r[0] for r in conn.execute(
                text("SELECT kind FROM event_rows WHERE run_id = :rid"),
                {"rid": seeded_row["run_id"]},
            ).fetchall()
        ]
    assert bool(row["passed"]) is True
    assert row["n_scores"] == 1  # inline heuristic only; no async score added
    assert "evaluator.failed_async" in kinds


def test_run_async_evaluator_logs_critical_when_row_vanished(
    seeded_row, worker_engine, caplog,
):
    """If the row's PK doesn't resolve (pruning, RLS mismatch), the
    worker must log CRITICAL and still emit a scored-async event
    that records ``row_updated=False`` so the audit trail captures
    the loss."""
    payload = _job_payload(
        seeded_row,
        ep_name="judge.mock_pointwise",
        ev_cfg={"id": "ghost", "score": 5.0, "threshold": 4.0},
    )
    # Synthesize a vanished row by deleting BEFORE running the worker.
    with worker_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM run_rows WHERE id = :pk"),
            {"pk": seeded_row["row_pk"]},
        )

    import logging
    with caplog.at_level(logging.CRITICAL, logger="evalguard.api.worker"):
        result = asyncio.run(run_async_evaluator({"engine": worker_engine}, payload))
    assert result["ok"] is True  # scoring itself succeeded
    # But the row update found zero rows; CRITICAL logged.
    crit = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert any("async_eval_row_not_found" in r.getMessage() for r in crit)
