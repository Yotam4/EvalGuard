"""Hash-chain audit primitives shared across packages.

Phase PROXY-3 carve-out.  The CLI's ``evalguard_cli.local.audit`` was
the original home for the audit log; the FastAPI server now needs to
emit the same kind of chain-linked event records for proxied
production calls, and pulling in the entire CLI for two helpers and
a constant would defeat the layering.

What lives here (pure logic — no storage, no actor, no I/O):

- ``EVENT_KINDS`` — the canonical event vocabulary mapped to W3C
  PROV classes.  Single source of truth shared by the CLI executor,
  the API proxy, and the run-schema drift test.
- ``redact_secrets`` — recursive key-name-based stripping for API
  keys, passwords, bearer tokens.  Applied before hashing so the
  chain commits to the redacted form.
- ``redact_privacy_payload`` — replace human-visible content with
  its hash+length when ``redact_payload=True``.
- ``build_event`` — assemble + hash one event given the previous
  chain tip.  The caller supplies actor identity, run/trial/row
  context, and PROV subject info; this function does the
  canonical-JSON hashing.
- ``verify_chain_events`` — walk a sorted event list and verify
  every link.  Decoupled from any specific store (the CLI passes
  ``SqliteStore.list_events(run_id)`` in; an API endpoint can pass
  whatever sequence it has).

What stays in the CLI's ``audit.py``:

- ``AuditLog`` — the stateful single-writer wrapper that knows
  about ``SqliteStore`` and ``Actor`` resolution.
- ``verify_chain(store, run_id)`` — thin shim that loads events
  from the store and hands them to ``verify_chain_events``.

The CLI's module re-exports every name in this file so existing
imports (and the three pinned drift tests) keep working unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Event vocabulary
# ---------------------------------------------------------------------------


# kind → (W3C PROV class, default subject_kind).
#
# Adding a new entry requires:
#   1. Append it here.
#   2. Update the drift assertion in
#      ``tests/test_pipeline_coverage.py`` that pins
#      ``distinct_kinds == set(EVENT_KINDS)``.
#   3. Update ``packages/schemas/evalguard.run.schema.json``
#      ``#/$defs/event.kind`` enum (which the schema-drift test pins).
#   4. If the new kind has a different PROV class than the existing
#      ones, update ``evalguard audit export``'s ``_to_prov_json`` so
#      it emits the right ``prov:type``.
EVENT_KINDS: dict[str, tuple[str, str | None]] = {
    "run.started":               ("Activity", "run"),
    "run.finalized":             ("Activity", "run"),
    "run.cost_capped":           ("Activity", "run"),
    "asset.resolved":            ("Entity",   "asset"),
    "trial.started":             ("Activity", "trial"),
    "trial.finalized":           ("Activity", "trial"),
    "row.short_circuited":       ("Activity", "row"),
    "provider.called":           ("Activity", "provider"),
    "provider.retry":            ("Activity", "provider"),
    "provider.failed":           ("Activity", "provider"),
    "evaluator.heuristic.invoked": ("Activity", "heuristic"),
    "evaluator.metric.invoked":  ("Activity", "metric"),
    "evaluator.judge.invoked":   ("Activity", "judge"),
    "gate.evaluated":            ("Activity", "gate"),
    "gate.custom_check.invoked": ("Activity", "custom_check"),
    # Layer-4 guardrail / judge_online enforcement on the proxy
    # /invoke path.  ``guardrail.blocked`` fires when an L4 gate
    # verdict refused the response (with mode=block); ``guardrail.timeout``
    # fires when an L4 evaluator exceeded its ``timeout_ms`` budget
    # (and we synthesised a Score per the ``on_timeout`` policy).
    # Both belong to the project chain; ``subject_id`` carries the
    # layer-gate id so the audit trail can pin which policy rule
    # acted.
    "guardrail.blocked":         ("Activity", "guardrail"),
    "guardrail.timeout":         ("Activity", "guardrail"),
    # Slice B: async evaluator dispatch via the Arq worker.
    # ``evaluator.scored_async`` fires when the worker successfully
    # scores a deferred evaluator and lands the score on the row;
    # ``evaluator.failed_async`` fires when the evaluator raised
    # OR the persist step failed (the row stays inline-only and
    # the audit trail records the loss).
    "evaluator.scored_async":    ("Activity", "evaluator"),
    "evaluator.failed_async":    ("Activity", "evaluator"),
}


# Fields that carry raw user/model content.  ``redact_privacy_payload``
# strips these from the payload before insert; their hashes remain on
# the event row so the chain still verifies post-erasure.
PRIVACY_SENSITIVE_FIELDS: tuple[str, ...] = (
    "rendered_prompt",
    "raw_response",
    "parsed_reason",
    "input",
    "output",
    "expected",
    "rubric",
)


# Keys whose values are *always* stripped from every event payload
# before storage — even when ``redact_payload`` is False.  The threat
# is API keys leaking from provider configs into the audit log: a YAML
# carrying ``api_key: ${OPENAI_API_KEY}`` would otherwise materialise
# the resolved secret into events.
#
# A substring/regex match generalises over vendor-specific names so we
# don't have to maintain a list as new providers are added.  Examples
# that match: ``api_key``, ``apikey``, ``API-KEY``, ``OPENAI_API_KEY``,
# ``MISTRAL_API_KEY``, ``GOOGLE_TOKEN``, ``my_password``,
# ``Authorization``, ``client_secret``, ``bearer_token``.
_SECRET_KEY_RE = re.compile(
    # Standalone forms (case-insensitive whole-string match).
    r"^(api[_-]?key|password|passwd|pwd|secret|authorization|credential|token)$"
    r"|"
    # Any key whose tail is a secret-shaped suffix.  ``token`` only
    # matches when *qualified* (``auth_token``, ``access_token``, …)
    # so ``tokens`` (LLM usage stats) is NOT redacted.
    r"(api[_-]?key|password|passwd|pwd|secret|authorization|credential|client[_-]?secret|"
    r"refresh[_-]?token|access[_-]?token|bearer[_-]?token|auth[_-]?token|"
    r"id[_-]?token|csrf[_-]?token)$",
    re.IGNORECASE,
)


def _is_secret_key(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    return bool(_SECRET_KEY_RE.search(name))


def redact_secrets(value: Any) -> Any:
    """Recursively replace known-sensitive values with ``"***"``.

    Walks dicts and lists; leaves scalars intact.  The match is on
    key name only — values aren't inspected, so a secret stored
    under an unexpected key name still leaks.  Treat as
    defense-in-depth, not a substitute for keeping secrets in env vars.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k) and v is not None:
                out[k] = "***"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


def redact_privacy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace each ``PRIVACY_SENSITIVE_FIELDS`` value in ``payload``
    with ``{redacted: True, sha256, length}`` so the chain still
    verifies after right-to-erasure removes the original content."""
    out = dict(payload)
    for field in PRIVACY_SENSITIVE_FIELDS:
        if field in out and out[field] is not None:
            text = _to_text(out[field])
            out[field] = {
                "redacted": True,
                "sha256":   _sha256_text(text),
                "length":   len(text),
            }
    return out


# ---------------------------------------------------------------------------
# Event construction (storage-agnostic)
# ---------------------------------------------------------------------------


def build_event(
    *,
    kind: str,
    run_id: str,
    prev_event_hash: str | None,
    actor_id: str,
    actor_type: str,
    actor_meta: dict[str, Any] | None = None,
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
    trace_id: str | None = None,
    redact_payload: bool = False,
) -> dict[str, Any]:
    """Assemble + hash one event given the chain's previous tip.

    The returned dict carries every field expected by the events
    table (CLI's SQLite store and the API's events shape).  Callers
    persist it; this function does not touch storage.

    Secret redaction (``redact_secrets``) runs on both
    ``actor_meta`` and ``payload`` before hashing — the chain
    commits to the redacted form, so leaked secrets can be removed
    without invalidating earlier verifications.  When
    ``redact_payload`` is True, ``PRIVACY_SENSITIVE_FIELDS`` are
    additionally replaced with their hash+length.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}; add it to EVENT_KINDS")
    if subject_kind is None:
        subject_kind = EVENT_KINDS[kind][1]

    safe_actor_meta = redact_secrets(dict(actor_meta or {}))
    safe_payload = redact_secrets(dict(payload or {}))
    if redact_payload:
        safe_payload = redact_privacy_payload(safe_payload)

    now_iso = _now_iso()
    record: dict[str, Any] = {
        "event_id":        _ulid(),
        "kind":            kind,
        "run_id":          run_id,
        "trial_id":        trial_id,
        "row_id":          row_id,
        "actor_id":        actor_id,
        "actor_type":      actor_type,
        "actor_meta":      safe_actor_meta,
        "subject_kind":    subject_kind,
        "subject_id":      subject_id,
        "subject_version": subject_version,
        "inputs_hash":     hash_canonical(inputs) if inputs is not None else None,
        "outputs_hash":    hash_canonical(outputs) if outputs is not None else None,
        "payload":         safe_payload,
        "cost_usd":        cost_usd,
        "started_at":      now_iso,
        "finished_at":     now_iso if duration_ms is not None else None,
        "duration_ms":     duration_ms,
        "trace_id":        trace_id,
        "span_id":         span_id or uuid.uuid4().hex[:16],
        "parent_span_id":  parent_span_id,
        "prev_event_hash": prev_event_hash,
    }
    record["event_hash"] = hash_event(record)
    return record


# ---------------------------------------------------------------------------
# Chain verification (storage-agnostic)
# ---------------------------------------------------------------------------


def verify_chain_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk a sorted event sequence and verify every link.

    The caller loads events from whichever store it owns; this
    function only cares about the per-event ``prev_event_hash`` →
    ``event_hash`` consistency.  Returns the same dict shape the
    CLI's ``verify_chain`` has always returned so callers stay
    drop-in compatible.
    """
    expected_prev: str | None = None
    for ev in events:
        if ev["prev_event_hash"] != expected_prev:
            return {
                "ok": False, "events": len(events),
                "broken_at": ev["event_id"],
                "reason": (
                    f"prev_event_hash mismatch at event {ev['event_id']}: "
                    f"expected {expected_prev!r}, got {ev['prev_event_hash']!r}"
                ),
            }
        recomputed = hash_event(ev)
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
# Hashing (canonical-JSON / sha256)
# ---------------------------------------------------------------------------


def hash_event(event: dict[str, Any]) -> str:
    """sha256 of the canonical-JSON event excluding ``event_hash``.

    The canonicalisation is
    ``json.dumps(sort_keys=True, separators=(',', ':'))`` over the
    event's other fields.  Deterministic across Python versions and
    implementations that sort string keys.
    """
    body = {k: v for k, v in event.items() if k != "event_hash"}
    return hash_canonical(body)


def hash_canonical(value: Any) -> str:
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
# Ids / timestamps
# ---------------------------------------------------------------------------


def _ulid() -> str:
    """Time-prefixed ULID-style id (sortable by emission time).

    26-char Crockford-base32.  We don't use ``ulid-py`` to keep the
    OSS dep surface small."""
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
