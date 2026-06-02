"""Persist + retrieve audit-chain events for proxy ingest.

Phase PROXY-3.5.  The relocated ``evalguard_evaluators.audit``
helpers (``build_event``, ``verify_chain_events``) are storage-
agnostic — they produce + verify event DICTS but don't know about
SQLAlchemy.  This module is the API-side adapter that:

- Reads the current chain tip for a given live run.
- Writes new events with proper ``prev_event_hash`` chaining, with
  IntegrityError-based retry so concurrent writers on the same run
  don't fork the chain.
- Reads events back for the verify + list endpoints.

The DB-shape contract: one row per event in ``event_rows``,
``UNIQUE (run_id, prev_event_hash)``.  That uniqueness is the
linchpin against forks: two concurrent writers both reading the
same tip will both compute a ``prev_event_hash`` equal to the tip's
hash; both INSERT; one wins, the other catches the unique violation
and retries with the now-advanced tip.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evalguard_api.db import now_iso
from evalguard_evaluators.audit import build_event


# Bound the retry loop.  A real production race would resolve in
# 1–2 retries; >5 retries means something pathological is happening
# (e.g., 50 concurrent writers on a tiny DB) and we'd rather surface
# the error than spin forever.  The caller's request times out at
# 60s anyway via the provider call's wait_for, but the audit insert
# itself shouldn't dominate.
_MAX_CHAIN_RETRIES: int = 8


def chain_tip_for_run(conn: Connection, run_id: str) -> str | None:
    """Return the most recently committed ``event_hash`` for the
    given run, or ``None`` when the chain is empty (first event of
    the day's live run).  Cheap: index-only seek on
    ``idx_event_rows_run(run_id, id DESC)``."""
    row = conn.execute(
        text("SELECT event_hash FROM event_rows "
             "WHERE run_id = :rid ORDER BY id DESC LIMIT 1"),
        {"rid": run_id},
    ).first()
    return row[0] if row else None


def emit_event(
    conn: Connection,
    *,
    kind: str,
    run_id: str,
    project_id: str,
    actor_id: str,
    actor_type: str,
    actor_meta: dict[str, Any] | None = None,
    trial_id: str | None = None,
    row_id: str | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    inputs: Any = None,
    outputs: Any = None,
    payload: dict[str, Any] | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    parent_span_id: str | None = None,
    span_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Append one chain-linked event to ``event_rows``.

    Re-reads the chain tip + retries on ``UNIQUE (run_id,
    prev_event_hash)`` violation so a concurrent writer racing on
    the same run produces a linear chain, not a fork.  Returns the
    committed event dict (the one ``verify_chain_events`` would
    re-hash).

    The caller hands us the actor identity + PROV subject context;
    we resolve ``prev_event_hash`` ourselves so callers don't have
    to think about chain serialisation.  This is the single
    write-site for the proxy's audit trail — invoke.py emits per
    call, no other route writes here.
    """
    last_err: Exception | None = None
    for attempt in range(_MAX_CHAIN_RETRIES):
        prev_hash = chain_tip_for_run(conn, run_id)
        record = build_event(
            kind=kind,
            run_id=run_id,
            prev_event_hash=prev_hash,
            actor_id=actor_id,
            actor_type=actor_type,
            actor_meta=actor_meta,
            trial_id=trial_id,
            row_id=row_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            inputs=inputs,
            outputs=outputs,
            payload=payload,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            parent_span_id=parent_span_id,
            span_id=span_id,
            trace_id=trace_id,
        )
        try:
            with conn.begin_nested():
                conn.execute(
                    text("""INSERT INTO event_rows(
                              event_id, run_id, trial_id, row_id, project_id,
                              kind, actor_id, actor_type,
                              subject_kind, subject_id,
                              cost_usd, duration_ms,
                              prev_event_hash, event_hash,
                              event_json, ingested_at)
                            VALUES (
                              :event_id, :run_id, :trial_id, :row_id, :project_id,
                              :kind, :actor_id, :actor_type,
                              :subject_kind, :subject_id,
                              :cost_usd, :duration_ms,
                              :prev, :hash,
                              :json_blob, :now)"""),
                    {
                        "event_id":    record["event_id"],
                        "run_id":      run_id,
                        "trial_id":    trial_id,
                        "row_id":      row_id,
                        "project_id":  project_id,
                        "kind":        kind,
                        "actor_id":    actor_id,
                        "actor_type":  actor_type,
                        "subject_kind": record["subject_kind"],
                        "subject_id":   subject_id,
                        "cost_usd":     cost_usd,
                        "duration_ms":  duration_ms,
                        "prev":         prev_hash,
                        "hash":         record["event_hash"],
                        "json_blob":    json.dumps(record, default=str),
                        "now":          now_iso(),
                    },
                )
            return record
        except IntegrityError as e:
            # Concurrent writer won the race for this chain tip.  Re-
            # read + re-compute + retry.  Loop is bounded.
            last_err = e
            continue
    # All retries lost — surface the error.  The caller is the proxy
    # invoke handler; this exception bubbles up and triggers the
    # phase-3-failure CRITICAL log (round-4 ticket).
    raise RuntimeError(
        f"audit chain insert lost {_MAX_CHAIN_RETRIES} retries for run "
        f"{run_id!r}; concurrent writers exhausted the budget"
    ) from last_err


def list_events_for_run(
    conn: Connection, run_id: str, limit: int = 500,
) -> list[dict[str, Any]]:
    """Return events for the given run in chain order (insertion order
    is the chain order — id ASC).  ``limit`` caps the read to keep
    the response bounded; chains beyond the cap need pagination on a
    future endpoint."""
    rows = conn.execute(
        text("SELECT event_json FROM event_rows "
             "WHERE run_id = :rid ORDER BY id ASC LIMIT :lim"),
        {"rid": run_id, "lim": limit},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for (blob,) in rows:
        try:
            out.append(json.loads(blob))
        except (TypeError, ValueError):
            # A malformed row shouldn't poison the whole list; the
            # verify endpoint will catch the hash mismatch separately.
            continue
    return out
