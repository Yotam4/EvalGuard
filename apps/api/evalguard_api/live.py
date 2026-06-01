"""Phase PROXY-2 — live (proxy) run helpers.

A proxied call's parent ``runs`` row is one per project per UTC
calendar day:

    run_id = "run_live" + sha256(project_id + "|" + YYYY-MM-DD)[:16]

Deterministic id means the first call of the day can lazy-create the
parent without a coordinator — concurrent first-of-day races collapse
via ``INSERT ... DO NOTHING`` (Postgres) / ``INSERT OR IGNORE``
(SQLite).  The hash form keeps the id 24 chars (run_ + 4 + 16 hex) so
it matches the existing run-id regex shape used elsewhere.

Trial id is similarly deterministic per ``(run, provider_id, model)``:

    trial_id = "trial_live" + sha256(run_id + "|" + provider_id + "|" + model)[:16]

so calls to the same provider+model land in the same trial and the
trial's aggregates accumulate naturally.

The ``ensure_*`` helpers do "create-if-missing" via a SAVEPOINT
INSERT — same pattern as ``db.py:upsert_project`` and
``routes/golden.py:promote``.  They return the canonical id and never
raise on a race.

``record_call`` is the per-call write-path: one row inserted, two
parent aggregates incremented.  Atomic at the SQL level so a
mid-flight error never leaves a row without an updated aggregate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evalguard_api.db import now_iso


# Mirror routes/runs.py:_PREVIEW_CHARS so live + batch rows truncate
# identically in the OBS-1 stream view.
_PREVIEW_CHARS: int = 240


def utc_date_str(now: datetime | None = None) -> str:
    """ISO calendar date in UTC (``YYYY-MM-DD``).  Defaults to
    ``datetime.now(timezone.utc)`` — injectable for tests that need
    to pin the day boundary."""
    n = now or datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%d")


def live_run_id(project_id: str, date_str: str) -> str:
    """Deterministic ``run_id`` for the day's live run.

    Hashing means the visible id is opaque (operators read the date
    off ``runs.started_at``, not the id), but the id stays a stable
    handle the proxy can re-derive from any call.  No metadata leaks
    across orgs because ``project_id`` is the only org-scoped input.
    """
    h = hashlib.sha256(f"{project_id}|{date_str}".encode("utf-8")).hexdigest()
    return f"run_live{h[:16]}"


def live_trial_id(run_id: str, provider_id: str, model: str) -> str:
    """Deterministic ``trial_id`` for one provider+model under a live
    run.  Two calls to the same provider+model land in the same trial
    so trial-level aggregates accumulate without per-call upsert
    races against new trial_ids."""
    h = hashlib.sha256(
        f"{run_id}|{provider_id}|{model}".encode("utf-8"),
    ).hexdigest()
    return f"trial_live{h[:16]}"


@dataclass(frozen=True)
class LiveCallRecord:
    """One proxied call's full record.  ``scores`` is the
    serialisable list emitted by the evaluator pass; ``raw_input`` /
    ``raw_expected`` are whatever the caller posted (passed through
    to the detail-json blob verbatim so the operator inspects exactly
    what the model saw)."""

    row_id:        str
    raw_input:     Any
    raw_expected:  Any
    output:        str
    passed:        bool
    n_scores:      int
    cost_usd:      float
    latency_ms:    int
    tags:          list[str] | None
    scores:        list[dict[str, Any]]
    provider:      str
    model:         str
    error:         str | None = None


def ensure_live_run(
    conn: Connection,
    *,
    project_id: str,
    project_name: str,
    date_str: str | None = None,
) -> str:
    """Get-or-create today's live run for the project.

    Idempotent under concurrency: a concurrent first-call-of-the-day
    that loses the INSERT race catches ``IntegrityError`` and returns
    the existing id.  No locking, no coordinator.
    """
    date_str = date_str or utc_date_str()
    run_id = live_run_id(project_id, date_str)

    existing = conn.execute(
        text("SELECT 1 FROM runs WHERE run_id = :rid"),
        {"rid": run_id},
    ).first()
    if existing:
        return run_id

    # Live runs carry a header-only payload by design — full per-row
    # detail lives on ``run_rows.detail_json`` (see migration 0011).
    # The header records enough that ``GET /v1/runs/{id}`` and the
    # OBS-1 stream view both render the run consistently.
    header = {
        "schema_version": "1.0.0",
        "run_id":         run_id,
        "project":        project_name,
        "kind":           "live",
        "source":         "live",
        "trials":         [],
    }
    started_at = f"{date_str}T00:00:00+00:00"
    try:
        with conn.begin_nested():
            conn.execute(
                text("""INSERT INTO runs(
                          run_id, project_id, project_name,
                          status, row_status, gate_status,
                          started_at, finished_at,
                          cost_usd, row_count, row_pass_count, row_fail_count,
                          payload_json, ingested_at, ingested_by, source)
                        VALUES (
                          :run_id, :project_id, :project_name,
                          'running', 'pending', 'pending',
                          :started_at, NULL,
                          0, 0, 0, 0,
                          :payload, :now, 'proxy', 'live')"""),
                {
                    "run_id":       run_id,
                    "project_id":   project_id,
                    "project_name": project_name,
                    "started_at":   started_at,
                    "payload":      json.dumps(header),
                    "now":          now_iso(),
                },
            )
    except IntegrityError:
        # Concurrent first-call won the race — the row exists.
        pass
    return run_id


def ensure_live_trial(
    conn: Connection,
    *,
    run_id: str,
    project_id: str,
    provider_id: str,
    provider: str,
    model: str,
) -> str:
    """Get-or-create the trial row for one provider+model under the
    day's live run.  Same SAVEPOINT-insert / catch-IntegrityError
    pattern as ``ensure_live_run``."""
    trial_id = live_trial_id(run_id, provider_id, model)

    existing = conn.execute(
        text("SELECT 1 FROM trials WHERE trial_id = :tid"),
        {"tid": trial_id},
    ).first()
    if existing:
        return trial_id

    try:
        with conn.begin_nested():
            conn.execute(
                text("""INSERT INTO trials(
                          trial_id, run_id, project_id,
                          provider_id, provider, model,
                          row_count, row_pass_count, row_fail_count,
                          cost_usd, status, gate_status, started_at)
                        VALUES (
                          :trial_id, :run_id, :project_id,
                          :provider_id, :provider, :model,
                          0, 0, 0,
                          0, 'running', 'pending', :now)"""),
                {
                    "trial_id":    trial_id,
                    "run_id":      run_id,
                    "project_id":  project_id,
                    "provider_id": provider_id,
                    "provider":    provider,
                    "model":       model,
                    "now":         now_iso(),
                },
            )
    except IntegrityError:
        pass
    return trial_id


def record_call(
    conn: Connection,
    *,
    run_id: str,
    trial_id: str,
    project_id: str,
    rec: LiveCallRecord,
) -> int:
    """Insert one ``run_rows`` row and increment the parent run +
    trial aggregates.  Returns the row's ``id``.

    The row insert + the two aggregate updates run in a single
    transaction (the FastAPI dependency hands us a single connection
    per request, ``deps.get_conn`` wraps it in ``begin``).  A mid-
    flight failure rolls back all three, so we never end up with a
    row whose aggregate counters disagree.
    """
    passed_int = 1 if rec.passed else 0
    output_preview = rec.output[:_PREVIEW_CHARS] if rec.output else None
    tags_json = json.dumps(rec.tags) if rec.tags else None
    # Defence-in-depth: a caller that POSTs an ``input`` carrying an
    # accidental ``api_key`` / ``password`` / ``authorization`` field
    # nested in a JSON dict shouldn't leak it via ``/calls/`` to anyone
    # with project read access.  ``redact_secrets`` is the same key-
    # name-based stripper the CLI audit chain uses (relocated to
    # evaluators by PROXY-3); it walks dicts + lists, leaves scalars
    # untouched, and is a no-op for the common case where ``input``
    # is just a string.  Operator-defined secrets are still their
    # responsibility (we can't infer "this string is sensitive"
    # without keying), but key-shaped leaks are caught.
    from evalguard_evaluators.audit import redact_secrets
    safe_input    = redact_secrets(rec.raw_input)
    safe_expected = redact_secrets(rec.raw_expected)
    detail_json = json.dumps({
        "input":    safe_input,
        "expected": safe_expected,
        "output":   rec.output,
        "scores":   rec.scores,
        "provider": rec.provider,
        "model":    rec.model,
        "error":    rec.error,
    }, default=str)

    ingested_at = now_iso()
    res = conn.execute(
        text("""INSERT INTO run_rows(
                  run_id, trial_id, project_id, row_id,
                  passed, n_scores, cost_usd, latency_ms, cache_hit,
                  tags_json, ingested_at, output_preview, detail_json)
                VALUES (
                  :run_id, :trial_id, :project_id, :row_id,
                  :passed, :n_scores, :cost, :latency, 0,
                  :tags, :now, :preview, :detail)
                RETURNING id"""
             if conn.dialect.name == "postgresql"
             else """INSERT INTO run_rows(
                  run_id, trial_id, project_id, row_id,
                  passed, n_scores, cost_usd, latency_ms, cache_hit,
                  tags_json, ingested_at, output_preview, detail_json)
                VALUES (
                  :run_id, :trial_id, :project_id, :row_id,
                  :passed, :n_scores, :cost, :latency, 0,
                  :tags, :now, :preview, :detail)"""),
        {
            "run_id":     run_id,
            "trial_id":   trial_id,
            "project_id": project_id,
            "row_id":     rec.row_id,
            "passed":     passed_int,
            "n_scores":   rec.n_scores,
            "cost":       float(rec.cost_usd),
            "latency":    int(rec.latency_ms),
            "tags":       tags_json,
            "now":        ingested_at,
            "preview":    output_preview,
            "detail":     detail_json,
        },
    )
    if conn.dialect.name == "postgresql":
        row_id_pk = res.scalar_one()
    else:
        row_id_pk = res.lastrowid

    # Aggregates.  All-in-one UPDATE per parent so the index lookup
    # on the PK only happens once per call (cheaper than three
    # separate SETs).
    conn.execute(
        text("""UPDATE runs
                SET row_count       = row_count + 1,
                    row_pass_count  = row_pass_count + :p,
                    row_fail_count  = row_fail_count + :f,
                    cost_usd        = cost_usd + :c
                WHERE run_id = :rid"""),
        {"p": passed_int, "f": 1 - passed_int,
         "c": float(rec.cost_usd), "rid": run_id},
    )
    conn.execute(
        text("""UPDATE trials
                SET row_count       = row_count + 1,
                    row_pass_count  = row_pass_count + :p,
                    row_fail_count  = row_fail_count + :f,
                    cost_usd        = cost_usd + :c
                WHERE trial_id = :tid"""),
        {"p": passed_int, "f": 1 - passed_int,
         "c": float(rec.cost_usd), "tid": trial_id},
    )

    return int(row_id_pk)


def parse_provider_id(provider_id: str) -> tuple[str, str]:
    """Mirror ``evalguard_cli.local.local_executor._split_provider_id``
    — ``'openai:gpt-4o-mini'`` → ``('openai', 'gpt-4o-mini')``; a
    bare ``'mock'`` → ``('mock', 'default')``."""
    if ":" in provider_id:
        a, b = provider_id.split(":", 1)
        return a, b
    return provider_id, "default"
