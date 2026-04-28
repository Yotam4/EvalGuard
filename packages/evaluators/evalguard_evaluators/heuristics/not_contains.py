"""Layer-1 heuristic: fail when the output contains a banned phrase."""

from __future__ import annotations

from typing import Any

from evalguard_evaluators.base import EvalContext, Score


class NotContainsHeuristic:
    kind = "heuristic"
    layer = 1

    def __init__(self) -> None:
        self.id: str = "not_contains"
        self._needles: list[str] = []
        self._case_sensitive: bool = False

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "not_contains")
        if "value" in cfg:
            self._needles = [cfg["value"]]
        elif "values" in cfg:
            self._needles = list(cfg["values"])
        else:
            raise ValueError("not_contains heuristic needs 'value' or 'values'")
        self._case_sensitive = bool(cfg.get("case_sensitive", False))

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        haystack = ctx.output if self._case_sensitive else ctx.output.lower()
        hits = []
        for needle in self._needles:
            n = needle if self._case_sensitive else needle.lower()
            if n in haystack:
                hits.append(needle)
        passed = not hits
        return [Score(self.id, self.kind, self.layer, 1.0 if passed else 0.0, passed, {"hits": hits})]
