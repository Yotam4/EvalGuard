"""Hash-chained, append-only audit log for an evalguard run.

Phase PROXY-3 carve-out: the pure-logic core (event vocabulary,
secret redaction, canonical-JSON hashing, chain verification) moved
to ``evalguard_evaluators.audit`` so the FastAPI server can use it
without dragging the CLI in.  This module is now a thin adapter
that:

- Owns the ``AuditLog`` class — the stateful single-writer emitter
  that knows about ``SqliteStore`` (for chain-tip lookup + event
  persistence) and ``Actor`` resolution (for CI / GitHub Actions
  identity attribution).
- Owns the ``AuditHook`` class — the per-evaluator bound emitter the
  executor hands to layer-by-layer evaluator invocations.
- Owns ``verify_chain(store, run_id)`` — the SqliteStore-loading
  wrapper that calls the shared ``verify_chain_events`` helper.
- Re-exports every public symbol the CLI / tests historically
  imported from this module, so the relocation is invisible to
  existing call sites (``EVENT_KINDS``, ``redact_secrets``, the
  ContextVar wrappers from ``evalguard_evaluators.audit_hook``).
"""

from __future__ import annotations

import contextvars
import uuid
from typing import Any

from evalguard_cli.local.actor import Actor, resolve_actor
from evalguard_cli.local.sqlite_store import SqliteStore

# Pure-logic core lives in evaluators-side now.  Re-export the public
# names so existing ``from evalguard_cli.local.audit import ...``
# call sites (executor, tests, audit_cmd) keep working unchanged.
from evalguard_evaluators.audit import (
    EVENT_KINDS,
    PRIVACY_SENSITIVE_FIELDS,
    build_event,
    hash_canonical,
    hash_event,
    redact_privacy_payload,
    redact_secrets,
    verify_chain_events,
)
# Same back-compat re-export pattern audit.py used before PROXY-3 —
# evaluators never need to import back into the CLI.
from evalguard_evaluators.audit_hook import (
    current_audit_hook as _evaluator_current_audit_hook,
    reset_audit_hook   as _evaluator_reset_audit_hook,
    set_audit_hook     as _evaluator_set_audit_hook,
)


# ---------------------------------------------------------------------------
# Emitter


class AuditLog:
    """Single-writer hash-chained event emitter for one run.

    Constructed once per run after the run row is created.  Caches
    the chain tip (from ``SqliteStore.last_event_hash``) and the
    actor identity so per-event emission is cheap.  Concurrent
    writers against the same ``run_id`` are unsupported by design —
    the in-memory ``_tip`` cache and the SQLite ``events`` table's
    append-only contract assume one writer per run.
    """

    def __init__(
        self,
        store: SqliteStore,
        run_id: str,
        *,
        actor: Actor | None = None,
        redact_payload: bool = False,
        project_dir: Any = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.actor = actor or resolve_actor(project_dir)
        self.redact_payload = redact_payload
        self.trace_id = uuid.uuid4().hex                 # 32 hex
        self._tip: str | None = store.last_event_hash(run_id)

    def emit(
        self,
        kind: str,
        *,
        trial_id: str | None = None,
        row_id: str | None = None,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        subject_version: str | None = None,
        inputs: Any = None,
        outputs: Any = None,
        payload: dict[str, Any] | None = None,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        parent_span_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one event.  Returns the event dict (post-hash).

        ``span_id`` may be pre-allocated by the caller so a sub-event
        (e.g. a judge's nested provider call) can record this event's
        span as its parent before this event is itself emitted.
        """
        record = build_event(
            kind=kind,
            run_id=self.run_id,
            prev_event_hash=self._tip,
            actor_id=self.actor.actor_id,
            actor_type=self.actor.actor_type,
            actor_meta=self.actor.actor_meta,
            trial_id=trial_id,
            row_id=row_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            subject_version=subject_version,
            inputs=inputs,
            outputs=outputs,
            payload=payload,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            parent_span_id=parent_span_id,
            span_id=span_id,
            trace_id=self.trace_id,
            redact_payload=self.redact_payload,
        )
        self.store.insert_event(record)
        self._tip = record["event_hash"]
        return record

    def _sanitize(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Back-compat shim — internal callers may have used this
        method directly.  Mirrors the legacy behaviour: only the
        privacy-field replacement (secret redaction now runs
        unconditionally inside ``build_event``)."""
        if not self.redact_payload:
            return payload
        return redact_privacy_payload(payload)


# ---------------------------------------------------------------------------
# Audit hook for nested events emitted by evaluators


class AuditHook:
    """Bound emitter handed to an evaluator before its invocation.

    The executor pre-allocates the evaluator's span_id and constructs
    an AuditHook bound to that span.  The evaluator can then emit
    nested events (e.g. a judge's own LLM call) with
    ``parent_span_id`` already set correctly, without needing to know
    the chain tip or actor identity.
    """

    def __init__(
        self,
        audit: "AuditLog",
        parent_span_id: str,
        *,
        trial_id: str | None,
        row_id: str | None,
    ) -> None:
        self._audit = audit
        self._parent_span_id = parent_span_id
        self._trial_id = trial_id
        self._row_id = row_id

    def emit_provider_call(
        self,
        *,
        provider: str,
        model: str,
        prompt: str,
        response: str,
        model_params: dict[str, Any] | None = None,
        tokens: dict[str, int] | None = None,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        raw: dict[str, Any] | None = None,
        is_judge_call: bool = False,
        cache_hit: bool = False,
    ) -> dict[str, Any]:
        """Record one LLM API call as a child of the current evaluator span."""
        return self._audit.emit(
            "provider.called",
            trial_id=self._trial_id,
            row_id=self._row_id,
            subject_id=f"{provider}:{model}",
            inputs=prompt,
            outputs=response,
            payload={
                "provider":        provider,
                "model":           model,
                "rendered_prompt": prompt,
                "raw_response":    response,
                "model_params":    model_params or {},
                "tokens":          tokens,
                "raw_provider":    raw or {},
                "is_judge_call":   is_judge_call,
                "cache_hit":       cache_hit,
            },
            cost_usd=cost_usd,
            duration_ms=latency_ms,
            parent_span_id=self._parent_span_id,
        )


# ---------------------------------------------------------------------------
# Task-local audit hook (avoids races on shared evaluator instances)


# When the executor invokes an evaluator under ``asyncio.gather`` with
# concurrency > 1, every coroutine shares the *same* evaluator instance.
# Storing the hook on ``ev._audit_hook`` would let one task's hook leak
# into another task's emit_provider_call.  ``ContextVar`` is task-local
# in asyncio, so each ``process_row`` coroutine gets its own view.
#
# The actual ContextVar lives in ``evalguard_evaluators.audit_hook`` so
# the dependency points cli → evaluators only — judges never need to
# import back into the CLI.  These wrappers preserve the original
# CLI-side names so the executor and tests don't have to change.


def current_audit_hook() -> "AuditHook | None":
    """Return the audit hook bound to the running evaluator, if any."""
    return _evaluator_current_audit_hook()


def set_audit_hook(hook: "AuditHook | None") -> "contextvars.Token[AuditHook | None]":
    """Bind an audit hook to the current task; returns a reset token."""
    return _evaluator_set_audit_hook(hook)


def reset_audit_hook(token: "contextvars.Token[AuditHook | None]") -> None:
    _evaluator_reset_audit_hook(token)


# ---------------------------------------------------------------------------
# Chain verification


def verify_chain(store: SqliteStore, run_id: str) -> dict[str, Any]:
    """Walk the per-run hash chain and verify every link.

    Loads events from the SqliteStore and delegates to the shared
    ``verify_chain_events`` helper.  Returns the same dict shape
    (``ok``, ``events``, ``broken_at``, ``reason``) callers have
    always relied on.
    """
    return verify_chain_events(store.list_events(run_id))


