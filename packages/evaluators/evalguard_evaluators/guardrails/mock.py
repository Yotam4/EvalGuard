"""Deterministic offline guardrail used by examples and tests.

Inspects the model's output for a configurable forbidden substring;
emits ``Score(layer=4, evaluator_kind="guardrail", passed=False)``
when the substring is present.  Real guardrails (Llama Guard, regex
PII detectors, classifier APIs) plug in via the same entry-point
group with whatever logic they need; this mock exists so the
``/invoke`` enforcement path can be exercised without an outbound
classifier call.

Config:

    guardrails:
      - type: mock
        forbidden: "secret"      # required: substring that triggers the block
        case_sensitive: false    # optional, default false
        threshold: 0.5           # optional: scoring threshold for the layer
                                 # gate (the score is 1.0 on pass / 0.0 on
                                 # fail, so any threshold in (0,1) blocks
                                 # on a match)
"""

from __future__ import annotations

from typing import Any

from evalguard_evaluators.base import EvalContext, Score


class MockGuardrail:
    kind = "guardrail"
    layer = 4

    def __init__(self) -> None:
        self.id: str = "mock"
        self._forbidden: str = ""
        self._case_sensitive: bool = False

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "mock")
        forbidden = cfg.get("forbidden")
        if not isinstance(forbidden, str) or not forbidden:
            raise ValueError(
                "guardrail.mock needs a non-empty 'forbidden' substring"
            )
        self._forbidden = forbidden
        self._case_sensitive = bool(cfg.get("case_sensitive", False))

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        haystack = ctx.output if self._case_sensitive else ctx.output.lower()
        needle = self._forbidden if self._case_sensitive else self._forbidden.lower()
        matched = needle in haystack
        return [
            Score(
                evaluator_id=self.id,
                evaluator_kind=self.kind,
                layer=self.layer,
                value=0.0 if matched else 1.0,
                passed=not matched,
                raw={"forbidden": self._forbidden, "matched": matched},
            )
        ]
