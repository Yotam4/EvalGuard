"""Hash-chained, append-only audit log for an evalguard run.

Goals:

- **Provenance** — every state change emits one event recording who did
  it, when, against which version-pinned asset, with which inputs and
  outputs. Vocabulary maps to W3C PROV (Activity / Entity / Agent).
- **Tamper evidence** — events form a per-``run_id`` hash chain. Each
  event's ``event_hash`` is sha256 of its canonical JSON; events also
  carry the ``prev_event_hash`` of the chain tip at insert time.
  ``verify_chain`` walks the chain and checks every link.
- **Privacy** — ``redact_payload=True`` strips human-visible content
  (rendered prompts, raw responses, judge reasons) but keeps content
  hashes so the chain still verifies post-erasure.

This module deliberately stores typed payloads as ``dict``s rather than
Pydantic models so the OSS tier doesn't take a Pydantic dependency
inside the hot path. The shape is documented in ``EVENT_KINDS``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from evalguard_cli.local.actor import Actor, resolve_actor
from evalguard_cli.local.sqlite_store import SqliteStore


# ---------------------------------------------------------------------------
# Event vocabulary

# kind → (W3C PROV class, default subject_kind)
EVENT_KINDS: dict[str, tuple[str, str | None]] = {
    "run.started":               ("Activity", "run"),
    "run.finalized":             ("Activity", "run"),
    "run.cost_capped":           ("Activity", "run"),
    "asset.resolved":            ("Entity",   "asset"),
    "trial.started":             ("Activity", "trial"),
    "trial.finalized":           ("Activity", "trial"),
    "row.short_circuited":       ("Activity", "row"),
    "provider.called":           ("Activity", "provider"),
    "evaluator.heuristic.invoked": ("Activity", "heuristic"),
    "evaluator.metric.invoked":  ("Activity", "metric"),
    "evaluator.judge.invoked":   ("Activity", "judge"),
    "gate.evaluated":            ("Activity", "gate"),
    "gate.custom_check.invoked": ("Activity", "custom_check"),
}

# Fields that contain raw user/model content. ``redact_payload`` strips
# these from payload before insert; their hashes remain on the event row.
_PRIVACY_SENSITIVE_FIELDS: tuple[str, ...] = (
    "rendered_prompt",
    "raw_response",
    "parsed_reason",
    "input",
    "output",
    "expected",
    "rubric",
)

# Keys whose values are *always* stripped from every event payload before
# storage — even when ``redact_payload`` is False. The threat is API keys
# leaking from provider configs into the audit log: a YAML containing
# ``api_key: ${OPENAI_API_KEY}`` would otherwise materialize the resolved
# secret into events. Match is case-insensitive on the key name.
_ALWAYS_REDACTED_KEYS: frozenset[str] = frozenset(map(str.lower, [
    "api_key", "apikey", "api-key",
    "token", "auth_token", "access_token", "refresh_token", "bearer_token",
    "password", "passwd", "pwd",
    "secret", "client_secret",
    "authorization",
    "openai_api_key", "anthropic_api_key", "ollama_api_key",
]))


def redact_secrets(value: Any) -> Any:
    """Recursively replace known-sensitive values with ``"***"``.

    Walks dicts and lists; leaves scalars intact. The match is on key
    name (case-insensitive) — values are not inspected, so a secret
    stored under an unexpected key name will still leak. Treat this as
    defense-in-depth, not a substitute for keeping secrets in env vars.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _ALWAYS_REDACTED_KEYS and v is not None:
                out[k] = "***"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Emitter


class AuditLog:
    """Single-writer hash-chained event emitter for one run.

    Construct once per run after the run row is started; it caches the
    chain tip and the actor identity so per-event emission is cheap.
    """

    def __init__(
        self,
        store: SqliteStore,
        run_id: str,
        *,
        actor: Actor | None = None,
        redact_payload: bool = False,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.actor = actor or resolve_actor()
        self.redact_payload = redact_payload
        self.trace_id = uuid.uuid4().hex                 # 32 hex
        self._tip: str | None = store.last_event_hash(run_id)

    # -- public ----------------------------------------------------------

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
        """Append one event. Returns the event dict (post-hash).

        ``span_id`` may be pre-allocated by the caller so a sub-event
        (e.g. a judge's nested provider call) can record this event's
        span as its parent before this event is itself emitted.
        """
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}; add it to EVENT_KINDS")

        if subject_kind is None:
            subject_kind = EVENT_KINDS[kind][1]

        # Strip API keys / tokens / passwords from payload + actor_meta
        # *before* hashing, so the chain commits to the redacted form.
        actor_meta = redact_secrets(self.actor.actor_meta)
        sanitized_payload = self._sanitize(redact_secrets(dict(payload or {})))

        now_iso = _now_iso()
        event_id = _ulid()
        if span_id is None:
            span_id = uuid.uuid4().hex[:16]                  # 16 hex
        record: dict[str, Any] = {
            "event_id":        event_id,
            "kind":            kind,
            "run_id":          self.run_id,
            "trial_id":        trial_id,
            "row_id":          row_id,
            "actor_id":        self.actor.actor_id,
            "actor_type":      self.actor.actor_type,
            "actor_meta":      actor_meta,
            "subject_kind":    subject_kind,
            "subject_id":      subject_id,
            "subject_version": subject_version,
            "inputs_hash":     _hash_canonical(inputs) if inputs is not None else None,
            "outputs_hash":    _hash_canonical(outputs) if outputs is not None else None,
            "payload":         sanitized_payload,
            "cost_usd":        cost_usd,
            "started_at":      now_iso,
            "finished_at":     now_iso if duration_ms is not None else None,
            "duration_ms":     duration_ms,
            "trace_id":        self.trace_id,
            "span_id":         span_id,
            "parent_span_id":  parent_span_id,
            "prev_event_hash": self._tip,
        }
        record["event_hash"] = _hash_event(record)
        self.store.insert_event(record)
        self._tip = record["event_hash"]
        return record

    # -- internals -------------------------------------------------------

    def _sanitize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.redact_payload:
            return payload
        out = dict(payload)
        for field in _PRIVACY_SENSITIVE_FIELDS:
            if field in out and out[field] is not None:
                out[field] = {
                    "redacted": True,
                    "sha256":   _sha256_text(_to_text(out[field])),
                    "length":   len(_to_text(out[field])),
                }
        return out


# ---------------------------------------------------------------------------
# Audit hook for nested events emitted by evaluators


class AuditHook:
    """Bound emitter handed to an evaluator before its invocation.

    The executor pre-allocates the evaluator's span_id and constructs
    an AuditHook bound to that span. The evaluator can then emit
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
# Chain verification


def verify_chain(store: SqliteStore, run_id: str) -> dict[str, Any]:
    """Walk the per-run hash chain and verify every link.

    Returns a dict with:
        ok (bool)        — whether every link verified
        events (int)     — number of events visited
        broken_at (str)  — event_id of the first failure (or None)
        reason (str)     — human-readable explanation
    """
    events = store.list_events(run_id)
    expected_prev: str | None = None
    for ev in events:
        # 1. prev pointer matches the chain so far
        if ev["prev_event_hash"] != expected_prev:
            return {
                "ok": False, "events": len(events),
                "broken_at": ev["event_id"],
                "reason": (
                    f"prev_event_hash mismatch at event {ev['event_id']}: "
                    f"expected {expected_prev!r}, got {ev['prev_event_hash']!r}"
                ),
            }
        # 2. event_hash matches recomputed canonical
        recomputed = _hash_event(ev)
        if recomputed != ev["event_hash"]:
            return {
                "ok": False, "events": len(events),
                "broken_at": ev["event_id"],
                "reason": (
                    f"event_hash mismatch at event {ev['event_id']}: "
                    f"recomputed {recomputed[:12]}…, stored {ev['event_hash'][:12]}…"
                ),
            }
        expected_prev = ev["event_hash"]
    return {"ok": True, "events": len(events), "broken_at": None, "reason": "chain intact"}


# ---------------------------------------------------------------------------
# Hashing


def _hash_event(event: dict[str, Any]) -> str:
    """sha256 of the canonical-JSON event excluding ``event_hash``.

    The canonicalization is ``json.dumps(sort_keys=True, separators=(',', ':'))``
    over the event's other fields. This is deterministic across Python
    versions and across implementations that sort string keys.
    """
    body = {k: v for k, v in event.items() if k != "event_hash"}
    return _hash_canonical(body)


def _hash_canonical(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# IDs


def _ulid() -> str:
    """Time-prefixed ULID-style id (sortable by emission time).

    26-char Crockford-base32. We don't use the ``ulid-py`` package to
    keep the OSS dep surface small.
    """
    ms = int(time.time() * 1000)
    rand_bits = uuid.uuid4().int & ((1 << 80) - 1)
    value = (ms << 80) | rand_bits
    return _crockford32(value, 26)


_C32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _crockford32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_C32[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
