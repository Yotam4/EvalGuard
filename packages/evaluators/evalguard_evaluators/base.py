"""Core types every evaluator and provider implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class EvalContext:
    """The data an evaluator sees for a single dataset row."""

    row_id: str
    input: Any
    expected: Any
    output: str
    provider: str
    model: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Score:
    """A single evaluator's verdict on one row."""

    evaluator_id: str
    evaluator_kind: str  # "heuristic" | "metric" | "judge" | "guardrail"
    layer: int           # 1..5  (4 == guardrail / judge_online)
    value: float         # heuristic: 0/1 ; judge: 1..5 (or rubric scale) ; metric: native ; guardrail: 0/1 or 0..1
    passed: bool
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Evaluator(Protocol):
    """All heuristics, metrics, judges, and guardrails share this protocol."""

    kind: str   # "heuristic" | "metric" | "judge" | "agent_metric" | "guardrail"
    layer: int

    def configure(self, cfg: dict[str, Any]) -> None: ...

    async def evaluate(self, ctx: EvalContext) -> list[Score]: ...


@dataclass(frozen=True)
class ProviderResult:
    output: str
    cost_usd: float
    latency_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """Wraps an LLM endpoint."""

    id: str

    def configure(self, cfg: dict[str, Any]) -> None: ...

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        params: dict[str, Any] | None = None,
    ) -> ProviderResult: ...
