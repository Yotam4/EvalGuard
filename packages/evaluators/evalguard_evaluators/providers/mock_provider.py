"""Offline provider: deterministic outputs derived from the input.

Useful for examples, CI, and tests where calling a real LLM is undesirable.
Behaviour is configurable via the ``mode`` field:

- ``echo``         return the input verbatim
- ``fixed``        return the verbatim string in ``output`` (e.g. canned SQL
  for the text_to_sql template demo). Per-row dataset overrides can swap
  this on individual rows.
- ``json_summary`` return ``{"summary": "...", "topic": "..."}`` shaped to
  match the quickstart example schema
- ``judge_score``  return ``{"score": <score>, "reason": "..."}`` parseable
  by ``PointwiseJudge``; the score is configurable via the ``score``
  config key (default 4.5).
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
        self._score: float = 4.5
        self._cost_per_call: float = 0.0
        self._fixed_output: str = ""

    def configure(self, cfg: dict[str, Any]) -> None:
        self._mode = cfg.get("mode", "json_summary")
        self._latency_ms = int(cfg.get("latency_ms", 5))
        self._score = float(cfg.get("score", 4.5))
        self._cost_per_call = float(cfg.get("cost_per_call", 0.0))
        self._fixed_output = str(cfg.get("output", ""))

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
        elif self._mode == "fixed":
            output = self._fixed_output
        elif self._mode == "json_summary":
            output = self._fake_summary(prompt)
        elif self._mode == "judge_score":
            output = json.dumps({"score": self._score, "reason": "mock judge response"})
        else:
            output = prompt[:200]
        elapsed = max(int((time.monotonic() - start) * 1000), self._latency_ms)
        return ProviderResult(
            output=output,
            cost_usd=self._cost_per_call,
            latency_ms=elapsed,
            raw={
                "provider": "mock", "mode": self._mode, "model": model,
                "usage": {"prompt_tokens": len(prompt) // 4,
                          "completion_tokens": len(output) // 4,
                          "total_tokens": (len(prompt) + len(output)) // 4},
            },
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
