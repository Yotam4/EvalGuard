"""Layer-1 heuristic: enforce length bounds (chars or tokens-ish)."""

from __future__ import annotations

from typing import Any

from evalguard_evaluators.base import EvalContext, Score


class LengthHeuristic:
    kind = "heuristic"
    layer = 1

    def __init__(self) -> None:
        self.id: str = "length"
        self._min: int | None = None
        self._max: int | None = None
        self._unit: str = "chars"

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "length")
        self._min = cfg.get("min")
        self._max = cfg.get("max")
        self._unit = cfg.get("unit", "chars")
        if self._unit not in {"chars", "words"}:
            raise ValueError(f"length unit must be 'chars' or 'words', got {self._unit!r}")
        if self._min is None and self._max is None:
            raise ValueError("length heuristic needs at least one of 'min' or 'max'")

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        n = len(ctx.output) if self._unit == "chars" else len(ctx.output.split())
        too_short = self._min is not None and n < self._min
        too_long = self._max is not None and n > self._max
        passed = not (too_short or too_long)
        raw = {"n": n, "unit": self._unit, "min": self._min, "max": self._max}
        return [Score(self.id, self.kind, self.layer, 1.0 if passed else 0.0, passed, raw)]
