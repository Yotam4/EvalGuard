"""Tests for the relocated audit core in ``evalguard_evaluators.audit``.

Phase PROXY-3.  The pure-logic helpers — EVENT_KINDS, redact_secrets,
build_event, verify_chain_events — now live in the evaluators package
so the FastAPI server can use them without depending on the CLI.

These tests pin the new contract directly; the existing
``tests/test_audit.py`` continues to cover the CLI-side AuditLog
adapter through ``from evalguard_cli.local.audit import ...``.
"""

from __future__ import annotations

from evalguard_evaluators.audit import (
    EVENT_KINDS, PRIVACY_SENSITIVE_FIELDS,
    build_event, hash_canonical, hash_event, redact_privacy_payload,
    redact_secrets, verify_chain_events,
)


# ---------------------------------------------------------------------------
# EVENT_KINDS contract


def test_event_kinds_includes_every_lifecycle_kind():
    """Drift canary at the evaluator-side import path.  This mirrors
    the existing test_pipeline_coverage.py drift assertion but pins
    the new module path so a future split / rename can't accidentally
    half-move the dict."""
    must_have = {
        "run.started", "run.finalized", "run.cost_capped",
        "asset.resolved",
        "trial.started", "trial.finalized",
        "row.short_circuited",
        "provider.called", "provider.retry", "provider.failed",
        "evaluator.heuristic.invoked", "evaluator.metric.invoked",
        "evaluator.judge.invoked",
        "gate.evaluated", "gate.custom_check.invoked",
        "guardrail.blocked", "guardrail.timeout",
        "evaluator.scored_async", "evaluator.failed_async",
    }
    assert must_have == set(EVENT_KINDS)


def test_event_kinds_re_exported_from_cli_module_is_same_object():
    """Identity check — the CLI's audit.py re-exports EVENT_KINDS
    from the evaluators side, so the two import paths must yield the
    *same* dict object (not a copy).  Otherwise an in-process mutation
    in one place wouldn't show up in the other."""
    from evalguard_cli.local.audit import EVENT_KINDS as cli_kinds
    assert cli_kinds is EVENT_KINDS


# ---------------------------------------------------------------------------
# redact_secrets


def test_redact_secrets_strips_known_key_names():
    out = redact_secrets({
        "api_key":     "evk_secret_abc",
        "password":    "hunter2",
        "OPENAI_API_KEY": "sk-xxx",
        "Authorization":  "Bearer evk_xxx",
        "model":       "gpt-4o-mini",      # not a secret
        "tokens":      {"prompt": 5},      # plural — not redacted
    })
    assert out["api_key"]       == "***"
    assert out["password"]      == "***"
    assert out["OPENAI_API_KEY"] == "***"
    assert out["Authorization"] == "***"
    assert out["model"]         == "gpt-4o-mini"
    assert out["tokens"]        == {"prompt": 5}


def test_redact_secrets_recurses_into_nested_structures():
    out = redact_secrets({
        "provider": {
            "config": {
                "api_key": "sk-abc",
                "endpoint": "https://api.example.com",
            },
        },
        "history": [
            {"api_key": "leaked-1"},
            {"api_key": "leaked-2"},
        ],
    })
    assert out["provider"]["config"]["api_key"]  == "***"
    assert out["provider"]["config"]["endpoint"] == "https://api.example.com"
    assert all(h["api_key"] == "***" for h in out["history"])


def test_redact_secrets_leaves_none_unchanged():
    """``None`` values are left alone — the redaction is "replace
    with ``***``" which would falsify "this key wasn't set" into
    "this key was set to a redacted value"."""
    out = redact_secrets({"api_key": None})
    assert out == {"api_key": None}


# ---------------------------------------------------------------------------
# redact_privacy_payload


def test_redact_privacy_payload_replaces_each_sensitive_field():
    out = redact_privacy_payload({
        "rendered_prompt": "Why was my order late?",
        "raw_response":    "We apologise…",
        "input":           "user input",
        "output":          "model output",
        "model":           "gpt-4o-mini",  # not in PRIVACY_SENSITIVE_FIELDS
    })
    # Privacy fields are replaced with hash+length…
    for f in ("rendered_prompt", "raw_response", "input", "output"):
        assert out[f]["redacted"] is True
        assert "sha256" in out[f]
        assert "length" in out[f]
    # …but unrelated fields stay verbatim.
    assert out["model"] == "gpt-4o-mini"


def test_privacy_sensitive_fields_covers_user_content_surfaces():
    """Drift assertion: every reader-facing content field used in the
    audit log must be on the privacy-strip list."""
    assert "rendered_prompt" in PRIVACY_SENSITIVE_FIELDS
    assert "raw_response"    in PRIVACY_SENSITIVE_FIELDS
    assert "input"           in PRIVACY_SENSITIVE_FIELDS
    assert "output"          in PRIVACY_SENSITIVE_FIELDS
    assert "expected"        in PRIVACY_SENSITIVE_FIELDS


# ---------------------------------------------------------------------------
# build_event + verify_chain_events end-to-end


def test_build_event_chains_correctly():
    """Three events: the second's prev_event_hash equals the first's
    event_hash; the third's equals the second's.  This is the
    contract the chain integrity check relies on."""
    ev1 = build_event(
        kind="run.started", run_id="run_x", prev_event_hash=None,
        actor_id="actor_a", actor_type="cli",
    )
    ev2 = build_event(
        kind="trial.started", run_id="run_x", prev_event_hash=ev1["event_hash"],
        actor_id="actor_a", actor_type="cli", trial_id="trial_t",
    )
    ev3 = build_event(
        kind="trial.finalized", run_id="run_x", prev_event_hash=ev2["event_hash"],
        actor_id="actor_a", actor_type="cli", trial_id="trial_t",
    )
    assert ev2["prev_event_hash"] == ev1["event_hash"]
    assert ev3["prev_event_hash"] == ev2["event_hash"]
    assert verify_chain_events([ev1, ev2, ev3])["ok"] is True


def test_build_event_redacts_secrets_in_actor_meta():
    """Secrets in actor metadata must be stripped BEFORE hashing so
    the chain commits to the redacted form."""
    ev = build_event(
        kind="run.started", run_id="run_x", prev_event_hash=None,
        actor_id="actor_a", actor_type="api_key",
        actor_meta={"api_key": "evk_secret_abc", "username": "alice"},
    )
    assert ev["actor_meta"]["api_key"] == "***"
    assert ev["actor_meta"]["username"] == "alice"


def test_build_event_rejects_unknown_kind():
    """A new event kind must be registered in EVENT_KINDS before
    use.  Catching this at construction time means a typo doesn't
    silently emit an unaudited event."""
    import pytest
    with pytest.raises(ValueError, match="unknown event kind"):
        build_event(
            kind="run.totally_invented", run_id="run_x",
            prev_event_hash=None,
            actor_id="actor_a", actor_type="cli",
        )


def test_verify_chain_events_detects_prev_hash_tamper():
    """Flip the second event's prev_event_hash → the walker reports
    ``broken_at`` at that event."""
    ev1 = build_event(
        kind="run.started", run_id="run_x", prev_event_hash=None,
        actor_id="a", actor_type="cli",
    )
    ev2 = build_event(
        kind="trial.started", run_id="run_x", prev_event_hash=ev1["event_hash"],
        actor_id="a", actor_type="cli",
    )
    # Tamper: pretend the previous event was different.
    ev2_tampered = dict(ev2, prev_event_hash="0" * 64)
    result = verify_chain_events([ev1, ev2_tampered])
    assert result["ok"] is False
    assert result["broken_at"] == ev2_tampered["event_id"]
    assert "prev_event_hash mismatch" in result["reason"]


def test_verify_chain_events_detects_event_hash_tamper():
    """Mutate the payload after the fact but leave event_hash alone →
    recomputed hash differs from stored hash → broken_at fires."""
    ev = build_event(
        kind="run.started", run_id="run_x", prev_event_hash=None,
        actor_id="a", actor_type="cli", payload={"x": 1},
    )
    # Tamper: the payload changed but event_hash wasn't recomputed.
    ev_tampered = dict(ev, payload={"x": 2})
    result = verify_chain_events([ev_tampered])
    assert result["ok"] is False
    assert "event_hash mismatch" in result["reason"]


def test_verify_chain_events_empty_list_is_ok():
    """A run with no audit events should verify as ok (vacuous truth)."""
    assert verify_chain_events([])["ok"] is True


# ---------------------------------------------------------------------------
# Hashing helpers — determinism


def test_hash_canonical_is_key_order_invariant():
    """``json.dumps(sort_keys=True)`` means the same dict always
    hashes to the same value regardless of insertion order — the
    contract the chain relies on."""
    a = hash_canonical({"x": 1, "y": 2})
    b = hash_canonical({"y": 2, "x": 1})
    assert a == b


def test_hash_event_excludes_event_hash_field():
    """The event's own ``event_hash`` field must NOT be part of its
    own hash input — otherwise the chain becomes self-referential."""
    ev = {"event_id": "x", "kind": "run.started", "payload": {}}
    h1 = hash_event(ev)
    h2 = hash_event({**ev, "event_hash": "doesnt-matter"})
    assert h1 == h2
