"""Background evaluator dispatch via Arq + Redis.

Slice B of the L4 / async / alerts roadmap.  ``/invoke`` runs L1 + L2
(+ L4 guardrails) inline because they need to gate the response;
heavier scorers (L3 ``judge_offline`` LLM judges, custom metrics
with model calls) take seconds and would multiply the proxy's p99
beyond what production hot-paths tolerate.  Marking such evaluators
``dispatch: async`` in the project YAML routes them through this
worker: the proxy returns the response immediately and the
evaluator's score lands on the same row a few seconds later.

What this module ships:

- ``WorkerSettings`` — the Arq worker definition.  ``functions``
  carries the single job ``run_async_evaluator``; ``redis_settings``
  is built from ``EVALGUARD_REDIS_URL`` at process start.
- ``run_async_evaluator`` — the job function.  Reconstructs an
  ``EvalContext`` from the queue payload, loads the evaluator from
  the same entry-point registry the inline path uses, scores it,
  and **updates the existing ``run_rows`` row in place** so the
  scores join their sibling inline scores.  Emits a
  ``evaluator.scored_async`` audit event so the chain records who
  scored what when.
- ``main`` — the console-script entry point.  ``uv run
  evalguard-evaluator-worker`` starts the worker.
- ``create_arq_pool`` — used by the API's lifespan to get a pool
  for ``enqueue_job`` calls from ``/invoke``.

Multi-tenant isolation: every job carries ``org_id`` in its payload;
the worker calls ``apply_rls_context(conn, org_id=..., is_admin=False)``
before any SELECT / UPDATE so a misrouted job cannot read or mutate
another tenant's rows.  A worker-side assert verifies the UPDATE
touched exactly one row; a count of zero would mean either the row
was deleted or RLS blocked the write (cross-org bug or pruning
race) and we log + emit ``evaluator.failed_async`` rather than
silently dropping the score.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from arq import cron
from arq import create_pool as _arq_create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.worker import Worker
from sqlalchemy import text
from sqlalchemy.engine import Engine

from evalguard_api.audit_persistence import emit_event
from evalguard_api.config import Settings, load_settings
from evalguard_api.db import apply_rls_context, make_engine

logger = logging.getLogger("evalguard.api.worker")


# ---------------------------------------------------------------------------
# Job payload
# ---------------------------------------------------------------------------


# Job-id template — Arq dedupes enqueue_job calls when the same
# ``_job_id`` is already pending.  ``row_id`` scopes the dedupe to
# one call; ``ep_name`` lets multiple async evaluators run for the
# same row without colliding.  A retry of the same call sees the
# same job_id and arq treats it as the same job (the right behaviour
# — we don't want a duplicate score from a client retry).
def job_id_for(row_id: str, ep_name: str) -> str:
    return f"asyncev:{row_id}:{ep_name}"


@dataclass(frozen=True)
class AsyncEvalJob:
    """Wire shape of one enqueue_job payload.

    Kept as a plain dict on the wire so arq's msgpack serialiser
    doesn't need to know about our dataclasses; this class is the
    typed contract for sender + receiver to agree on.
    """

    project_id: str
    org_id: str
    run_id: str
    trial_id: str
    row_id: str
    row_pk: int               # the ``run_rows.id`` primary key
    ep_name: str              # e.g. "judge.pointwise"
    ev_cfg: dict[str, Any]    # what gets passed to load_evaluator
    eval_context: dict[str, Any]  # serialised EvalContext fields
    actor_key_id: str
    actor_scopes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id":   self.project_id,
            "org_id":       self.org_id,
            "run_id":       self.run_id,
            "trial_id":     self.trial_id,
            "row_id":       self.row_id,
            "row_pk":       self.row_pk,
            "ep_name":      self.ep_name,
            "ev_cfg":       self.ev_cfg,
            "eval_context": self.eval_context,
            "actor_key_id": self.actor_key_id,
            "actor_scopes": list(self.actor_scopes),
        }


# ---------------------------------------------------------------------------
# Pool factory
# ---------------------------------------------------------------------------


def _redis_settings(redis_url: str) -> RedisSettings:
    """Build an Arq ``RedisSettings`` from a URL like ``redis://host:6379/0``.

    Centralised so tests can monkeypatch the resolver without
    re-implementing the URL parsing.
    """
    return RedisSettings.from_dsn(redis_url)


async def create_arq_pool(redis_url: str) -> ArqRedis:
    """Create an Arq pool the API can use to enqueue jobs.

    Returns the live pool; callers are responsible for calling
    ``await pool.aclose()`` on shutdown (the API lifespan does this).
    """
    return await _arq_create_pool(_redis_settings(redis_url))


# ---------------------------------------------------------------------------
# Job function
# ---------------------------------------------------------------------------


async def run_async_evaluator(ctx: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Run one evaluator out-of-band and persist its scores onto the
    existing ``run_rows`` row.

    ``ctx`` is Arq's task context (carries the worker's shared state
    including the engine we attached in ``startup``).  ``job`` is
    the ``AsyncEvalJob.as_dict()`` payload the proxy enqueued.

    Returns a small dict describing what happened — Arq stores it on
    the job result for operator inspection (``arq``'s own
    ``redis-cli`` introspection helpers).
    """
    # Re-acquire workspace types lazily so a missing optional dep
    # surfaces here as a worker-side ``evaluator.failed_async``
    # event rather than crashing the worker process.
    from evalguard_evaluators.base import EvalContext, Score
    from evalguard_evaluators.registry import load_evaluator

    engine: Engine = ctx["engine"]
    payload = AsyncEvalJob(**{k: job[k] for k in (
        "project_id", "org_id", "run_id", "trial_id", "row_id",
        "row_pk", "ep_name", "ev_cfg", "eval_context",
        "actor_key_id", "actor_scopes",
    )})

    # Reconstruct EvalContext.  Worker has no notion of the original
    # FastAPI request body — every field we need was captured at
    # enqueue time.
    eval_ctx = EvalContext(
        row_id=payload.eval_context["row_id"],
        input=payload.eval_context.get("input"),
        expected=payload.eval_context.get("expected"),
        output=payload.eval_context.get("output", ""),
        provider=payload.eval_context.get("provider", ""),
        model=payload.eval_context.get("model", ""),
        extra=payload.eval_context.get("extra") or {},
    )

    # Run the evaluator OUTSIDE the DB transaction so a slow scorer
    # doesn't hold a connection for its full duration.  The job
    # contract is "scores arrive eventually", so the per-call audit
    # event lands before the connection is reacquired.
    try:
        ev = load_evaluator(payload.ep_name, payload.ev_cfg)
        scores: list[Score] = await ev.evaluate(eval_ctx)
    except Exception as e:  # noqa: BLE001 — surface anything the evaluator throws
        return _record_evaluator_failure(
            engine, payload,
            error=f"{type(e).__name__}: {e}",
            stage="evaluate",
        )

    # Render scores into the same dict shape the inline path writes
    # so the read-side aggregator (``CallDetail``, ``/calls/`` panel)
    # treats async + inline scores uniformly.
    score_dicts = [
        {
            "evaluator_id":   s.evaluator_id,
            "evaluator_kind": s.evaluator_kind,
            "layer":          s.layer,
            "value":          s.value,
            "passed":         s.passed,
            "raw":            s.raw,
        }
        for s in scores
    ]
    all_passed = all(s["passed"] for s in score_dicts) if score_dicts else True

    try:
        with engine.begin() as conn:
            apply_rls_context(conn, org_id=payload.org_id, is_admin=False)
            updated = _merge_scores_into_row(
                conn, payload.row_pk, score_dicts, new_passed=all_passed,
            )
            if updated == 0:
                # The row vanished (pruning?) or RLS blocked the
                # write (org_id drift bug).  Log loud + emit the
                # failure event in a NEW transaction so the audit
                # trail captures it (this transaction is otherwise
                # a no-op).
                logger.critical(
                    '{"evt":"async_eval_row_not_found","row_pk":%d,'
                    '"row_id":%r,"project_id":%r,"org_id":%r,"ep_name":%r}',
                    payload.row_pk, payload.row_id,
                    payload.project_id, payload.org_id, payload.ep_name,
                )
            emit_event(
                conn,
                kind="evaluator.scored_async",
                run_id=payload.run_id,
                project_id=payload.project_id,
                trial_id=payload.trial_id,
                row_id=payload.row_id,
                actor_id=payload.actor_key_id,
                actor_type="api_key",
                actor_meta={"scopes": list(payload.actor_scopes)},
                subject_id=payload.ep_name,
                payload={
                    "ep_name":     payload.ep_name,
                    "scores":      score_dicts,
                    "all_passed":  all_passed,
                    "row_updated": bool(updated),
                },
            )
    except Exception as e:  # noqa: BLE001
        return _record_evaluator_failure(
            engine, payload,
            error=f"{type(e).__name__}: {e}",
            stage="persist",
        )

    return {
        "ok":         True,
        "ep_name":    payload.ep_name,
        "score_count": len(score_dicts),
        "all_passed": all_passed,
    }


def _merge_scores_into_row(
    conn,
    row_pk: int,
    new_scores: list[dict[str, Any]],
    *,
    new_passed: bool,
) -> int:
    """SELECT + parse + UPDATE the ``run_rows`` row.

    Async scores merge into ``detail_json.scores`` (alongside the
    inline scores) under a dedicated ``async_scores`` key so the
    read-side can distinguish them and the inline scores stay
    immutable.  ``n_scores`` increments; ``passed`` becomes
    ``passed AND new_passed`` — async scores can only DEGRADE the
    verdict, never restore it (inline failures already locked
    ``passed=False`` and the operator deliberately gated on them).

    Returns the number of rows updated (0 ⇒ row gone / RLS-blocked).
    """
    row = conn.execute(
        text("""SELECT passed, n_scores, detail_json, run_id, trial_id
                FROM run_rows WHERE id = :pk"""),
        {"pk": row_pk},
    ).mappings().fetchone()
    if row is None:
        return 0

    try:
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
    except (TypeError, ValueError):
        detail = {}
    async_bucket = detail.get("async_scores") or {}
    for s in new_scores:
        # Keyed by evaluator_id so a re-enqueue of the same async
        # job overwrites cleanly instead of duplicating the score.
        async_bucket[s["evaluator_id"]] = s
    detail["async_scores"] = async_bucket

    old_passed = bool(row["passed"])
    combined_passed = old_passed and new_passed
    passed_int = 1 if combined_passed else 0
    n_added = len(new_scores)

    conn.execute(
        text("""UPDATE run_rows
                SET detail_json = :detail,
                    passed      = :p,
                    n_scores    = n_scores + :n
                WHERE id = :pk"""),
        {
            "detail": json.dumps(detail, default=str),
            "p":      passed_int,
            "n":      n_added,
            "pk":     row_pk,
        },
    )

    # Aggregate maintenance — only act when async scoring FLIPS
    # the row from passed to failed.  passed→passed and
    # failed→failed are no-ops on the aggregates.  failed→passed
    # is impossible (combined_passed = old AND new).
    if old_passed and not combined_passed:
        conn.execute(
            text("""UPDATE runs
                    SET row_pass_count = row_pass_count - 1,
                        row_fail_count = row_fail_count + 1
                    WHERE run_id = :rid"""),
            {"rid": row["run_id"]},
        )
        conn.execute(
            text("""UPDATE trials
                    SET row_pass_count = row_pass_count - 1,
                        row_fail_count = row_fail_count + 1
                    WHERE trial_id = :tid"""),
            {"tid": row["trial_id"]},
        )

    return 1


def _record_evaluator_failure(
    engine: Engine,
    payload: AsyncEvalJob,
    *,
    error: str,
    stage: str,
) -> dict[str, Any]:
    """Emit ``evaluator.failed_async`` so the audit trail records the
    loss; don't touch the row's scores (they remain inline-only)."""
    try:
        with engine.begin() as conn:
            apply_rls_context(conn, org_id=payload.org_id, is_admin=False)
            emit_event(
                conn,
                kind="evaluator.failed_async",
                run_id=payload.run_id,
                project_id=payload.project_id,
                trial_id=payload.trial_id,
                row_id=payload.row_id,
                actor_id=payload.actor_key_id,
                actor_type="api_key",
                actor_meta={"scopes": list(payload.actor_scopes)},
                subject_id=payload.ep_name,
                payload={
                    "ep_name": payload.ep_name,
                    "stage":   stage,
                    "error":   error,
                },
            )
    except Exception:  # noqa: BLE001
        # If even the failure event can't land, we've lost
        # everything except the structured log.  Better than
        # silently crashing the worker.
        logger.critical(
            '{"evt":"async_eval_failed_unrecorded","row_id":%r,'
            '"ep_name":%r,"stage":%r,"error":%r}',
            payload.row_id, payload.ep_name, stage, error,
        )
    return {"ok": False, "stage": stage, "error": error}


# ---------------------------------------------------------------------------
# Arq worker shape
# ---------------------------------------------------------------------------


async def _worker_startup(ctx: dict[str, Any]) -> None:
    """Acquire the shared SQLAlchemy engine the worker re-uses across
    jobs.  Mirrors the API's lifespan engine setup; we deliberately
    DON'T re-run Alembic migrations here (the API owns schema)."""
    settings: Settings = ctx.get("settings") or load_settings()
    ctx["settings"] = settings
    ctx["engine"]   = make_engine(settings)
    logger.info(
        "EvalGuard async-evaluator worker ready · dialect=%s",
        ctx["engine"].dialect.name,
    )


async def _worker_shutdown(ctx: dict[str, Any]) -> None:
    engine: Engine | None = ctx.get("engine")
    if engine is not None:
        engine.dispose()


async def cron_evaluate_alerts(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron entry — re-evaluates every project's alert rules.

    Lives on the same Arq worker as ``run_async_evaluator`` so we
    don't need a second process or scheduler.  Errors per rule are
    isolated by the engine; this wrapper only handles the worker-
    level shape.
    """
    from evalguard_api.alerts import evaluate_all_alert_rules
    engine: Engine = ctx["engine"]
    outcomes = await evaluate_all_alert_rules(engine)
    return {
        "checked":   len(outcomes),
        "fired":     sum(1 for o in outcomes if o.fired),
        "resolved":  sum(1 for o in outcomes if o.resolved),
        "suppressed": sum(1 for o in outcomes if o.suppressed),
    }


class WorkerSettings:
    """Arq ``WorkerSettings`` for ``evalguard-evaluator-worker``.

    The class form is the convention Arq looks for when invoked via
    ``arq evalguard_api.worker.WorkerSettings`` — keeping it on the
    module surface so operators can run the worker either through
    our console script or through Arq's own CLI.
    """

    functions = [run_async_evaluator]
    # Slice C: re-evaluate every project's alert rules once a minute.
    # Per-rule windows can be longer (10m, 1h, 24h) — the cron just
    # decides "is it time to check?", the engine does the actual
    # window math.  Sub-minute windows aren't supported in v1; if
    # they ever are, switch to ``cron(..., second={0, 30})``.
    cron_jobs = [cron(cron_evaluate_alerts, minute=set(range(0, 60)))]
    on_startup = _worker_startup
    on_shutdown = _worker_shutdown
    max_tries = 3
    job_timeout = 300  # 5 minutes — generous for a slow judge LLM

    @classmethod
    def _resolve_redis_settings(cls) -> RedisSettings:
        return _redis_settings(os.environ.get("EVALGUARD_REDIS_URL", ""))


def main() -> None:
    """Console-script entry point: start the Arq worker.

    Reads ``EVALGUARD_REDIS_URL`` (required) from the environment;
    a missing URL is a refusal, not a silent fallback, because the
    operator who started the worker process explicitly asked for
    Redis-backed dispatch.
    """
    url = os.environ.get("EVALGUARD_REDIS_URL")
    if not url:
        raise SystemExit(
            "EVALGUARD_REDIS_URL is not set.  The async-evaluator worker "
            "requires a Redis broker to dequeue jobs; set the URL or run "
            "the API in inline-only mode (don't start the worker)."
        )
    WorkerSettings.redis_settings = _redis_settings(url)  # type: ignore[attr-defined]
    worker = Worker(
        functions=WorkerSettings.functions,
        cron_jobs=WorkerSettings.cron_jobs,
        redis_settings=WorkerSettings.redis_settings,  # type: ignore[attr-defined]
        on_startup=WorkerSettings.on_startup,
        on_shutdown=WorkerSettings.on_shutdown,
        max_tries=WorkerSettings.max_tries,
        job_timeout=WorkerSettings.job_timeout,
    )
    asyncio.run(worker.async_run())
