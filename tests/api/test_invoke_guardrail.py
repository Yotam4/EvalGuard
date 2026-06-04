"""End-to-end coverage for layer-4 (``judge_online``) guardrail
enforcement on the ``/invoke`` proxy path.

These tests mirror the ``test_invoke_cost_cap_*`` shape: push a
config that wires up a guardrail + refusal policy, invoke with
inputs that do / don't trip the guardrail, and assert (a) the HTTP
response code, (b) the response body shape, (c) the row landed in
``/calls/?tab=failures``, and (d) the audit chain emitted the right
event kinds.

The ``MockGuardrail`` evaluator (``evalguard_evaluators.guardrails.mock``)
gives us a deterministic substring-match block without an outbound
classifier call, so the suite stays air-gap-clean.
"""

from __future__ import annotations

import asyncio
from typing import Any


# ---------------------------------------------------------------------------
# YAML config helpers


def _config(
    *,
    forbidden: str = "blocked-word",
    mode: str = "block",
    refusal_mode: str | None = "http_error",
    refusal_status: int | None = None,
    refusal_text: str | None = None,
    timeout_ms: int = 1000,
    on_timeout: str = "fail_open",
) -> str:
    refusal_block = ""
    if refusal_mode is not None:
        refusal_block = "refusal_response:\n"
        refusal_block += f"  mode: {refusal_mode}\n"
        if refusal_status is not None:
            refusal_block += f"  status: {refusal_status}\n"
        if refusal_text is not None:
            refusal_block += f"  text: {refusal_text!r}\n"
    return (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "guardrails:\n"
        "  - id: policy-1\n"
        f"    type: mock\n"
        f"    forbidden: {forbidden!r}\n"
        f"    timeout_ms: {timeout_ms}\n"
        f"    on_timeout: {on_timeout}\n"
        "layers:\n"
        "  judge_online:\n"
        "    severity: block\n"
        f"    mode: {mode}\n"
        + refusal_block
    )


def _push(client, headers, content: str) -> None:
    r = client.post(
        "/v1/projects/default/config",
        json={"content": content},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# Happy path: clean input passes through


def test_invoke_clean_input_passes_guardrail(client, auth_headers) -> None:
    _push(client, auth_headers, _config())
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "perfectly safe input"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True
    assert body["blocked_by"] is None
    # The guardrail still ran — its passing score is on the response.
    guardrail_scores = [s for s in body["scores"] if s["evaluator_kind"] == "guardrail"]
    assert len(guardrail_scores) == 1
    assert guardrail_scores[0]["passed"] is True
    assert guardrail_scores[0]["layer"] == 4


# ---------------------------------------------------------------------------
# Block path — http_error mode


def test_invoke_blocked_http_error_returns_403(client, auth_headers) -> None:
    _push(client, auth_headers, _config(refusal_mode="http_error"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "this contains the blocked-word inside"},
        headers=auth_headers,
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["passed"] is False
    assert body["error"] is not None
    assert "guardrail_blocked" in body["error"]
    assert body["blocked_by"] == "judge_online"


def test_invoke_blocked_http_error_respects_custom_status(client, auth_headers) -> None:
    _push(client, auth_headers, _config(refusal_mode="http_error", refusal_status=451))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "trigger via blocked-word here"},
        headers=auth_headers,
    )
    # 451 Unavailable For Legal Reasons — a policy-honest choice.
    assert r.status_code == 451, r.text


def test_invoke_blocked_row_lands_in_failures_tab(client, auth_headers) -> None:
    _push(client, auth_headers, _config(refusal_mode="http_error"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "blocked-word again"},
        headers=auth_headers,
    )
    body = r.json()
    failures = client.get(
        "/v1/projects/default/calls?tab=failures&limit=5",
        headers=auth_headers,
    )
    assert failures.status_code == 200
    assert any(c["row_id"] == body["row_id"] for c in failures.json()["calls"])


# ---------------------------------------------------------------------------
# Block path — http_200_refusal mode


def test_invoke_blocked_http_200_refusal_returns_200_with_refusal_text(
    client, auth_headers,
) -> None:
    _push(
        client, auth_headers,
        _config(
            refusal_mode="http_200_refusal",
            refusal_text="I can't help with that.",
        ),
    )
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "contains blocked-word — refuse"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output"] == "I can't help with that."
    assert body["passed"] is False
    assert body["blocked_by"] == "judge_online"
    # The error field is None on the 200-refusal path — the refusal
    # is the body itself, not an HTTP-level error.
    assert body["error"] is None


def test_invoke_blocked_http_200_refusal_uses_default_text(client, auth_headers) -> None:
    _push(client, auth_headers, _config(refusal_mode="http_200_refusal"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "blocked-word"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    # Default refusal text from the schema default.
    assert "blocked" in r.json()["output"].lower()


# ---------------------------------------------------------------------------
# Warn / log modes (don't block but annotate / record)


def test_invoke_warn_mode_does_not_block_but_marks_failed(client, auth_headers) -> None:
    _push(client, auth_headers, _config(mode="warn"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "contains blocked-word"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Original provider output preserved (no refusal substitution).
    assert "blocked-word" in body["output"]
    # But the row is marked failed so the operator sees it in /calls/.
    assert body["passed"] is False
    # No blocked_by — only ``block`` mode populates that field.
    assert body["blocked_by"] is None


def test_invoke_log_mode_records_silently(client, auth_headers) -> None:
    _push(client, auth_headers, _config(mode="log"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "contains blocked-word"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Output preserved; row stays passed=True (the score is recorded
    # but the layer's mode chose not to mark the row failed).
    assert "blocked-word" in body["output"]
    # Find the guardrail score on the response — it should still
    # carry passed=False so a downstream consumer can rebuild the
    # verdict even though the gate didn't fire.
    g = [s for s in body["scores"] if s["evaluator_kind"] == "guardrail"]
    assert g[0]["passed"] is False


# ---------------------------------------------------------------------------
# Timeout policy


def _slow_evaluator_config(on_timeout: str, timeout_ms: int = 20) -> str:
    """A guardrail with an absurdly low ``timeout_ms`` so we can
    deterministically exercise the timeout branch.  The ``forbidden``
    substring never matches (output is "x"), but the timeout fires
    long before the substring check completes."""
    # MockGuardrail's evaluate is too fast to time out on its own; we
    # inject a slow custom evaluator via monkeypatch instead — see
    # ``test_invoke_guardrail_timeout_*`` below.
    return (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "guardrails:\n"
        "  - id: slow-policy\n"
        "    type: slow_mock\n"
        f"    timeout_ms: {timeout_ms}\n"
        f"    on_timeout: {on_timeout}\n"
        "layers:\n"
        "  judge_online:\n"
        "    severity: block\n"
        "    mode: block\n"
        "refusal_response:\n"
        "  mode: http_error\n"
    )


def _install_slow_guardrail(monkeypatch) -> None:
    """Register a ``guardrail.slow_mock`` evaluator that sleeps for
    a full second before returning a pass.  ``timeout_ms=20`` in the
    config aborts it; the on_timeout policy decides whether the
    request is allowed through."""
    from evalguard_evaluators.base import EvalContext, Score
    from evalguard_evaluators import registry

    class _SlowGuardrail:
        kind = "guardrail"
        layer = 4

        def __init__(self) -> None:
            self.id = "slow_mock"

        def configure(self, cfg: dict[str, Any]) -> None:
            self.id = cfg.get("id", "slow_mock")

        async def evaluate(self, ctx: EvalContext) -> list[Score]:
            await asyncio.sleep(1.0)
            return [Score(self.id, self.kind, self.layer, 1.0, True, {})]

    # Replace the cached evaluator map so the registry returns the
    # slow class for ``guardrail.slow_mock`` lookups in this test.
    registry._evaluator_classes.cache_clear()
    real_loader = registry._evaluator_classes

    def _patched() -> dict[str, type]:
        m = dict(real_loader.__wrapped__())  # type: ignore[attr-defined]
        m["guardrail.slow_mock"] = _SlowGuardrail
        return m

    monkeypatch.setattr(registry, "_evaluator_classes", _patched)


def test_invoke_guardrail_timeout_fail_open_allows_request(
    client, auth_headers, monkeypatch,
) -> None:
    _install_slow_guardrail(monkeypatch)
    _push(client, auth_headers, _slow_evaluator_config(on_timeout="fail_open"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "anything"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True
    # The synthesised timeout score lands on the response so the
    # operator can see the guardrail outage in /calls/.
    g = [s for s in body["scores"] if s["evaluator_kind"] == "guardrail"]
    assert len(g) == 1
    assert g[0]["raw"].get("synthesised") is True
    assert g[0]["raw"].get("on_timeout") == "fail_open"


def test_invoke_guardrail_timeout_fail_closed_blocks_request(
    client, auth_headers, monkeypatch,
) -> None:
    _install_slow_guardrail(monkeypatch)
    _push(client, auth_headers, _slow_evaluator_config(on_timeout="fail_closed"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "anything"},
        headers=auth_headers,
    )
    # fail_closed + mode=block + refusal_response.mode=http_error → 403.
    assert r.status_code == 403, r.text
    body = r.json()
    assert "guardrail_blocked" in (body.get("error") or "")
    assert body["blocked_by"] == "judge_online"


# ---------------------------------------------------------------------------
# Audit chain


def test_invoke_blocked_emits_guardrail_blocked_audit_event(
    client, auth_headers,
) -> None:
    _push(client, auth_headers, _config(refusal_mode="http_error"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "blocked-word"},
        headers=auth_headers,
    )
    assert r.status_code == 403
    run_id = r.json()["run_id"]
    events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}&limit=50",
        headers=auth_headers,
    )
    assert events.status_code == 200, events.text
    kinds = [e["kind"] for e in events.json()["events"]]
    assert "guardrail.blocked" in kinds
    # The provider event itself is ``provider.failed`` here because the
    # proxy stamped ``error = "guardrail_blocked: ..."`` before Phase 3,
    # which the existing audit emitter reads as a failed call.  The
    # provider's *upstream* call succeeded — the failure is the proxy's
    # policy refusal — and the dedicated ``guardrail.blocked`` event
    # carries the policy-level reason so the chain doesn't conflate
    # the two failure modes.
    assert "provider.failed" in kinds


def test_invoke_timeout_emits_guardrail_timeout_audit_event(
    client, auth_headers, monkeypatch,
) -> None:
    _install_slow_guardrail(monkeypatch)
    _push(client, auth_headers, _slow_evaluator_config(on_timeout="fail_open"))
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "anything"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}&limit=50",
        headers=auth_headers,
    )
    assert events.status_code == 200, events.text
    kinds = [e["kind"] for e in events.json()["events"]]
    assert "guardrail.timeout" in kinds


# ---------------------------------------------------------------------------
# No-guardrails-configured baseline (regression: L4 is opt-in)


def test_invoke_without_guardrails_block_works_unchanged(client, auth_headers) -> None:
    """Configs that don't set ``guardrails:`` keep their pre-L4
    behaviour — no inline enforcement, no refusal, ``blocked_by`` is
    null on every response."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
    )
    _push(client, auth_headers, cfg)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "this would have been blocked-word"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True
    assert body["blocked_by"] is None
