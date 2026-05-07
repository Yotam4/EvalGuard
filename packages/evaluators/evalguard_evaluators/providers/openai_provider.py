"""OpenAI Chat Completions provider.

Also drives **OpenAI-compatible local servers** (Ollama's ``/v1`` shim,
vLLM, LM Studio, llama.cpp's ``server``, LocalAI, Together AI, Groq, …)
by setting ``base_url`` in the provider config. ``api_key`` becomes
optional in that case — local servers typically don't require one.
"""

from __future__ import annotations

import os
import time
from typing import Any

from evalguard_evaluators.base import ProviderResult


# Pricing table is intentionally tiny and easy to extend. Prices in USD per
# 1M tokens. Keep this file the single source of truth so cost accounting is
# auditable from one place.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o":          (2.50, 10.00),
    "gpt-4o-mini":     (0.15, 0.60),
    "gpt-4.1":         (2.00, 8.00),
    "gpt-4.1-mini":    (0.40, 1.60),
}


class OpenAIProvider:
    id = "openai"

    # 60s is generous for an eval-row LLM call but well under the
    # OpenAI SDK's default ~10 minutes — a hung connection would
    # otherwise block a row long enough that retry can't recover.
    DEFAULT_TIMEOUT_S: float = 60.0

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._base_url: str | None = None
        self._default_params: dict[str, Any] = {}
        self._timeout_s: float = self.DEFAULT_TIMEOUT_S
        self._client: Any = None  # AsyncOpenAI, lazy-init in _get_client()

    def configure(self, cfg: dict[str, Any]) -> None:
        self._api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
        self._base_url = cfg.get("base_url")
        self._timeout_s = float(cfg.get("timeout_s", self.DEFAULT_TIMEOUT_S))
        # ``retry`` is an evalguard-operational key the executor
        # normally strips before reaching here; filter it again as
        # defense-in-depth so a direct caller (or a future code path)
        # never accidentally forwards it as a SDK kwarg, which the
        # OpenAI client would reject.
        #
        # ``model`` is filtered too — when a user puts ``model:
        # gpt-4o-mini`` in the provider config block (the natural
        # spot for it), it would otherwise be merged into
        # ``_default_params`` and passed twice to
        # ``chat.completions.create``, raising
        # ``TypeError: got multiple values for keyword argument 'model'``.
        # The model arrives via the ``provider:model`` id at call time;
        # the config-block copy is informational only.
        # ``timeout_s`` is consumed above; strip it so the SDK never
        # sees an unknown kwarg.
        self._default_params = {
            k: v for k, v in cfg.items()
            if k not in {"api_key", "base_url", "retry", "model", "timeout_s"}
        }
        self._client = None  # invalidate on reconfigure

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError(
                "openai SDK not installed. `pip install evalguard-evaluators[openai]`"
            ) from e
        if not self._api_key and not self._base_url:
            raise RuntimeError("OPENAI_API_KEY not set and no api_key in provider config")
        self._client = AsyncOpenAI(api_key=self._api_key or "local", base_url=self._base_url)
        return self._client

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        params: dict[str, Any] | None = None,
    ) -> ProviderResult:
        client = self._get_client()
        merged = {**self._default_params, **(params or {})}
        start = time.monotonic()
        # Per-call ``timeout`` overrides the SDK's default; the OpenAI
        # client raises on timeout with a message that the default
        # ``RetryPolicy`` already pattern-matches, so a hung connection
        # surfaces as a normal retryable error.
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=self._timeout_s,
            **merged,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        choice = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        in_tokens = getattr(usage, "prompt_tokens", 0) or 0
        out_tokens = getattr(usage, "completion_tokens", 0) or 0
        in_price, out_price = _PRICING.get(model, (0.0, 0.0))
        cost = (in_tokens * in_price + out_tokens * out_price) / 1_000_000
        return ProviderResult(
            output=choice,
            cost_usd=round(cost, 6),
            latency_ms=elapsed,
            raw={
                "provider": "openai",
                "model": model,
                "in_tokens": in_tokens,
                "out_tokens": out_tokens,
            },
        )
