"""Offline provider: deterministic outputs derived from the input.

Useful for examples, CI, and tests where calling a real LLM is undesirable.
Behaviour is configurable via the ``mode`` field:

- ``echo``     return the input verbatim
- ``expected`` return the row's ``expected`` field (set on the call)
- ``json_summary`` return ``{"summary": "...", "topic": "..."}`` shaped to
  match the quickstart example schema
"""

from __future__ import annotations

import json
import time
from typing import Any

from evalguard_evaluators.base import ProviderResult


class MockProvider:
    id = "mock"

    def __init__(self) -> None:
        self._mode: str = "json_summary"
        self._latency_ms: int = 5

    def configure(self, cfg: dict[str, Any]) -> None:
        self._mode = cfg.get("mode", "json_summary")
        self._latency_ms = int(cfg.get("latency_ms", 5))

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        params: dict[str, Any] | None = None,
    ) -> ProviderResult:
        start = time.monotonic()
        if self._mode == "echo":
            output = prompt
        elif self._mode == "json_summary":
            output = self._fake_summary(prompt)
        else:
            output = prompt[:200]
        elapsed = max(int((time.monotonic() - start) * 1000), self._latency_ms)
        return ProviderResult(
            output=output,
            cost_usd=0.0,
            latency_ms=elapsed,
            raw={"provider": "mock", "mode": self._mode, "model": model},
        )

    @staticmethod
    def _fake_summary(prompt: str) -> str:
        # Pull the first line of the article (after "Article:") as the topic
        topic = ""
        for marker in ("Article:", "ARTICLE:", "Text:"):
            if marker in prompt:
                rest = prompt.split(marker, 1)[1].strip()
                topic = rest.splitlines()[0][:80] if rest else ""
                break
        if not topic:
            topic = prompt.strip().splitlines()[0][:80] if prompt.strip() else "unknown"
        summary = f"Summary of: {topic}"
        return json.dumps({"summary": summary, "topic": topic[:40]})
