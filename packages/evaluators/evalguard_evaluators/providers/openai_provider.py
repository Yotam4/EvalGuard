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

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._base_url: str | None = None
        self._default_params: dict[str, Any] = {}
        self._client: Any = None  # AsyncOpenAI, lazy-init in _get_client()

    def configure(self, cfg: dict[str, Any]) -> None:
        self._api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
        self._base_url = cfg.get("base_url")
        self._default_params = {k: v for k, v in cfg.items() if k not in {"api_key", "base_url"}}
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
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
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
