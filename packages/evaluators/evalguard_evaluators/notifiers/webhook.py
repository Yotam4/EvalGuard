"""Generic webhook notifier — POSTs the alert payload as JSON.

Wire shape: the body is ``AlertPayload.as_dict()`` serialised as
JSON.  When a ``secret`` is configured, the notifier signs the body
with HMAC-SHA256 and attaches the signature in an
``X-EvalGuard-Signature`` header — operators verify incoming
deliveries by re-computing the HMAC over the raw body using the
same secret.  Without the secret the header is omitted; integrations
that don't need signed deliveries (Zapier, n8n behind auth) can
skip it.

Failure modes are absorbed: a non-2xx response or a network error
is recorded in ``NotifyResult.ok=False`` rather than raised, so a
single broken notifier doesn't poison the whole alert dispatch
loop.  Retries belong in the alert engine, not the notifier.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from evalguard_evaluators.notifiers.base import (
    AlertPayload, NotifyResult,
)


class WebhookNotifier:
    kind = "webhook"

    def __init__(self) -> None:
        self._url: str = ""
        self._secret: str | None = None
        self._timeout_s: float = 5.0
        self._headers: dict[str, str] = {}

    def configure(self, cfg: dict[str, Any]) -> None:
        url = cfg.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("webhook notifier needs a non-empty 'url'")
        self._url = url
        secret = cfg.get("secret")
        self._secret = secret if isinstance(secret, str) and secret else None
        self._timeout_s = float(cfg.get("timeout_s", 5.0))
        extra_headers = cfg.get("headers") or {}
        if not isinstance(extra_headers, dict):
            raise ValueError("webhook notifier 'headers' must be a mapping")
        self._headers = {str(k): str(v) for k, v in extra_headers.items()}

    async def send(self, payload: AlertPayload) -> NotifyResult:
        body = json.dumps(payload.as_dict(), default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent":   "evalguard-webhook/1.0",
            **self._headers,
        }
        if self._secret is not None:
            sig = hmac.new(
                self._secret.encode("utf-8"),
                body, hashlib.sha256,
            ).hexdigest()
            headers["X-EvalGuard-Signature"] = f"sha256={sig}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as cx:
                r = await cx.post(self._url, content=body, headers=headers)
        except Exception as e:  # noqa: BLE001
            return NotifyResult(
                kind=self.kind, ok=False,
                detail=f"{type(e).__name__}: {e}",
            )
        if r.status_code >= 400:
            return NotifyResult(
                kind=self.kind, ok=False,
                detail=f"HTTP {r.status_code}: {r.text[:200]}",
            )
        return NotifyResult(
            kind=self.kind, ok=True,
            detail=f"HTTP {r.status_code}",
        )
