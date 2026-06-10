"""Unit tests for the notifier registry + shipped notifiers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import pytest

from evalguard_evaluators.notifiers.base import AlertPayload
from evalguard_evaluators.notifiers.mock import MockNotifier
from evalguard_evaluators.notifiers.webhook import WebhookNotifier
from evalguard_evaluators.registry import (
    iter_notifiers, load_notifier, reset_registry_cache,
)


def _payload() -> AlertPayload:
    return AlertPayload(
        schema="evalguard.alert.v1",
        rule_id="r1", project_id="proj_x",
        fired_at="2026-06-10T12:00:00+00:00",
        window="15m", gate="pass_rate",
        observed_value=0.5, threshold={"min": 0.9},
        transition="pass_to_fail",
    )


# ---------------------------------------------------------------------------
# Registry


def test_notifiers_registered_via_entry_points():
    reset_registry_cache()
    m = iter_notifiers()
    assert "mock" in m
    assert "webhook" in m
    assert m["mock"] is MockNotifier
    assert m["webhook"] is WebhookNotifier


def test_load_notifier_returns_configured_instance():
    n = load_notifier("mock", {"label": "ci-mock"})
    assert isinstance(n, MockNotifier)
    # MockNotifier records the label on send.
    MockNotifier.reset()
    result = asyncio.run(n.send(_payload()))
    assert result.ok is True
    assert "ci-mock" in (result.detail or "")
    assert len(MockNotifier.sent) == 1


def test_load_notifier_unknown_raises():
    with pytest.raises(KeyError, match="unknown notifier"):
        load_notifier("nonexistent", {})


# ---------------------------------------------------------------------------
# Webhook notifier — uses an in-process httpx mock transport so no
# real network call happens.


def _httpx_handler(captured: dict) -> Any:
    """Build a callable accepted by ``httpx.MockTransport``."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(204)
    return handler


def test_webhook_notifier_posts_json(monkeypatch):
    import httpx

    captured: dict = {}

    class _MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_httpx_handler(captured))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "evalguard_evaluators.notifiers.webhook.httpx.AsyncClient",
        _MockClient,
    )
    n = WebhookNotifier()
    n.configure({"url": "https://example.test/hook"})
    result = asyncio.run(n.send(_payload()))
    assert result.ok is True
    req = captured["request"]
    assert str(req.url) == "https://example.test/hook"
    body = json.loads(req.content.decode("utf-8"))
    assert body["rule_id"] == "r1"
    assert body["transition"] == "pass_to_fail"
    assert "X-EvalGuard-Signature" not in req.headers


def test_webhook_notifier_signs_with_hmac_when_secret_set(monkeypatch):
    import httpx

    captured: dict = {}

    class _MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_httpx_handler(captured))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "evalguard_evaluators.notifiers.webhook.httpx.AsyncClient",
        _MockClient,
    )
    n = WebhookNotifier()
    n.configure({
        "url": "https://example.test/hook",
        "secret": "hunter2",
    })
    asyncio.run(n.send(_payload()))
    req = captured["request"]
    sig_header = req.headers.get("X-EvalGuard-Signature", "")
    assert sig_header.startswith("sha256=")
    expected = hmac.new(
        b"hunter2", req.content, hashlib.sha256,
    ).hexdigest()
    assert sig_header == f"sha256={expected}"


def test_webhook_notifier_returns_failure_on_5xx(monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    class _MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "evalguard_evaluators.notifiers.webhook.httpx.AsyncClient",
        _MockClient,
    )
    n = WebhookNotifier()
    n.configure({"url": "https://example.test/hook"})
    result = asyncio.run(n.send(_payload()))
    assert result.ok is False
    assert "503" in (result.detail or "")


def test_webhook_notifier_rejects_empty_url():
    n = WebhookNotifier()
    with pytest.raises(ValueError, match="url"):
        n.configure({})
