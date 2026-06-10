"""End-to-end tests for ``dispatch: async`` evaluator routing.

Strategy: rather than spin up Arq + Redis (or fakeredis end-to-end),
we install a **fake Arq pool** on ``app.state.arq_pool`` that just
records every ``enqueue_job`` call.  The unit tests in
``test_worker_job.py`` separately verify the worker function does
the right thing when handed a payload; this file verifies the
proxy enqueues the right payload to begin with.
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeArqPool:
    """Minimal stub mimicking ``arq.connections.ArqRedis.enqueue_job``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_next = False

    async def enqueue_job(self, function: str, *args, _job_id: str | None = None, **kw):
        if self.raise_on_next:
            self.raise_on_next = False
            raise RuntimeError("redis unreachable (simulated)")
        self.calls.append({
            "function": function,
            "args":     args,
            "job_id":   _job_id,
            "kwargs":   kw,
        })

    async def aclose(self) -> None:  # API lifespan calls this on shutdown
        return None


def _config_with_async_judge() -> str:
    return (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "judges:\n"
        "  - id: async-j\n"
        "    type: mock_pointwise\n"
        "    score: 4.5\n"
        "    threshold: 4.0\n"
        "    dispatch: async\n"
    )


def _push(client, headers, content: str) -> None:
    r = client.post(
        "/v1/projects/default/config", json={"content": content},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# Happy path


def test_invoke_async_evaluator_is_enqueued_not_run_inline(
    client, auth_headers,
):
    """An evaluator tagged ``dispatch: async`` skips inline scoring,
    records a ``pending`` placeholder in the response, and lands one
    ``enqueue_job`` call on the pool."""
    pool = _FakeArqPool()
    client.app.state.arq_pool = pool

    _push(client, auth_headers, _config_with_async_judge())
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "ping"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Exactly one enqueue call, routed to the worker function.
    assert len(pool.calls) == 1
    call = pool.calls[0]
    assert call["function"] == "run_async_evaluator"
    assert call["job_id"].startswith("asyncev:")
    assert call["job_id"].endswith(":judge.mock_pointwise")

    # The job payload carries everything the worker needs.
    job = call["args"][0]
    assert job["ep_name"] == "judge.mock_pointwise"
    assert job["org_id"]
    assert job["row_id"] == body["row_id"]
    assert job["row_pk"] > 0
    assert job["ev_cfg"]["id"] == "async-j"
    # ``dispatch`` MUST be stripped from the ev_cfg the worker sees
    # (otherwise ``MockPointwiseJudge.configure`` would receive an
    # unexpected field).
    assert "dispatch" not in job["ev_cfg"]
    assert job["eval_context"]["row_id"] == body["row_id"]
    assert job["actor_key_id"]

    # The inline scores list carries a pending placeholder for the
    # async judge so /calls/ can show "judge.mock_pointwise: pending".
    pending = [
        s for s in body["scores"]
        if s["evaluator_id"] == "async-j" and s["raw"].get("pending") is True
    ]
    assert len(pending) == 1


def test_invoke_mixes_inline_and_async_evaluators_correctly(
    client, auth_headers,
):
    """One inline + one async evaluator in the same config: the
    inline one's score appears on the response, the async one is
    enqueued, and the placeholder still ships."""
    pool = _FakeArqPool()
    client.app.state.arq_pool = pool
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "heuristics:\n"
        "  - id: len-inline\n"
        "    type: length\n"
        "    max: 10000\n"
        "judges:\n"
        "  - id: judge-async\n"
        "    type: mock_pointwise\n"
        "    score: 5.0\n"
        "    threshold: 4.0\n"
        "    dispatch: async\n"
    )
    _push(client, auth_headers, cfg)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "hi"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Exactly one async enqueue (the judge).
    assert len(pool.calls) == 1
    assert pool.calls[0]["args"][0]["ep_name"] == "judge.mock_pointwise"
    # Two score entries on the response: the inline heuristic + the pending judge.
    kinds = sorted({s["evaluator_kind"] for s in body["scores"]})
    assert kinds == ["heuristic", "judge"]


# ---------------------------------------------------------------------------
# Refusals


def test_invoke_rejects_async_dispatch_when_no_pool_configured(
    client, auth_headers,
):
    """No ``EVALGUARD_REDIS_URL`` ⇒ ``app.state.arq_pool is None`` ⇒
    any ``dispatch: async`` in the config trips a 503."""
    client.app.state.arq_pool = None
    _push(client, auth_headers, _config_with_async_judge())
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 503, r.text
    assert "Redis" in r.json()["detail"]


def test_invoke_rejects_async_dispatch_on_guardrails(client, auth_headers):
    """Layer-4 guardrails MUST run inline.  Schema can't easily forbid
    the field in YAML-loose mode, so the proxy refuses at request time."""
    pool = _FakeArqPool()
    client.app.state.arq_pool = pool
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "guardrails:\n"
        "  - id: bad-async-guardrail\n"
        "    type: mock\n"
        "    forbidden: 'x'\n"
        "    timeout_ms: 100\n"
        "    on_timeout: fail_open\n"
        "    dispatch: async\n"
        "layers:\n"
        "  judge_online:\n"
        "    severity: block\n"
        "    mode: block\n"
        "refusal_response:\n"
        "  mode: http_error\n"
    )
    _push(client, auth_headers, cfg)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert "guardrail" in r.json()["detail"].lower()
    # No enqueue happened.
    assert pool.calls == []


# ---------------------------------------------------------------------------
# Enqueue failure mode


def test_invoke_logs_when_enqueue_fails_but_call_still_returns_200(
    client, auth_headers, caplog,
):
    """If Redis throws during enqueue, the proxy logs a warning but
    still returns the inline portion of the call (provider output
    already happened — we don't punish the customer for a broker
    outage).  Score-on-row will be missing the async judge."""
    pool = _FakeArqPool()
    pool.raise_on_next = True
    client.app.state.arq_pool = pool
    _push(client, auth_headers, _config_with_async_judge())
    import logging
    with caplog.at_level(logging.WARNING, logger="evalguard.api.invoke"):
        r = client.post(
            "/v1/projects/default/invoke",
            json={"input": "x"},
            headers=auth_headers,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # No enqueued calls landed (raise_on_next was True).
    assert pool.calls == []
    # Pending placeholder still on the response (we don't roll it back).
    pending = [s for s in body["scores"] if s["raw"].get("pending") is True]
    assert len(pending) == 1
    # The structured warning landed.
    assert any("async_enqueue_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Schema sanity (default inline)


def test_invoke_without_dispatch_field_runs_inline_unchanged(
    client, auth_headers,
):
    """Configs that don't set ``dispatch`` keep the pre-Slice-B
    behaviour: every evaluator runs inline, ``arq_pool`` is never
    touched."""
    pool = _FakeArqPool()
    client.app.state.arq_pool = pool
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "judges:\n"
        "  - id: inline-j\n"
        "    type: mock_pointwise\n"
        "    score: 5.0\n"
        "    threshold: 4.0\n"
    )
    _push(client, auth_headers, cfg)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    # No enqueue calls.
    assert pool.calls == []
    body = r.json()
    # Judge score landed inline.
    j = [s for s in body["scores"] if s["evaluator_id"] == "inline-j"]
    assert len(j) == 1
    assert j[0]["passed"] is True
    assert j[0]["raw"].get("pending") is not True
