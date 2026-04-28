"""Ollama provider — talk to a local Ollama server (default localhost:11434).

Cost is reported as $0 since the model is local. ``model`` is the Ollama
tag, e.g. ``llama3.2:3b`` or ``qwen2.5:7b``.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from evalguard_evaluators.base import ProviderResult


class OllamaProvider:
    id = "ollama"

    def __init__(self) -> None:
        self._base_url: str = "http://localhost:11434"
        self._timeout: float = 120.0
        self._default_options: dict[str, Any] = {}

    def configure(self, cfg: dict[str, Any]) -> None:
        self._base_url = (
            cfg.get("base_url")
            or os.environ.get("OLLAMA_HOST")
            or "http://localhost:11434"
        ).rstrip("/")
        self._timeout = float(cfg.get("timeout", 120.0))
        self._default_options = cfg.get("options", {})

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        params: dict[str, Any] | None = None,
    ) -> ProviderResult:
        options = {**self._default_options, **(params or {})}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": options,
        }
        url = f"{self._base_url}/api/chat"
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            data = r.json()
        elapsed = int((time.monotonic() - start) * 1000)
        output = (data.get("message") or {}).get("content", "")
        return ProviderResult(
            output=output,
            cost_usd=0.0,
            latency_ms=elapsed,
            raw={
                "provider": "ollama",
                "model": model,
                "base_url": self._base_url,
                "eval_count": data.get("eval_count"),
                "prompt_eval_count": data.get("prompt_eval_count"),
                "total_duration_ns": data.get("total_duration"),
            },
        )
