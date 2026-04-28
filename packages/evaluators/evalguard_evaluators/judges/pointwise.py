"""Layer-3 LLM-as-judge: G-Eval-style pointwise rubric scoring.

Two implementations:
- ``PointwiseJudge`` calls a real provider (OpenAI by default).
- ``MockPointwiseJudge`` returns a deterministic, configurable score so
  examples and tests can run offline. This is what the quickstart uses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evalguard_evaluators.base import EvalContext, Score
from evalguard_evaluators.registry import load_provider

_DEFAULT_TEMPLATE = """You are a strict evaluator.

Rubric:
{rubric}

Input:
{input}

Model output:
{output}

{expected_block}

Reply ONLY with a JSON object:
{{"score": <integer 1-5>, "reason": "<one sentence>"}}
"""

_EXPECTED_BLOCK = "Reference answer (for comparison only):\n{expected}\n"


def _render_input(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


class PointwiseJudge:
    kind = "judge"
    layer = 3

    def __init__(self) -> None:
        self.id: str = "pointwise"
        self._rubric: str = ""
        self._threshold: float = 4.0
        self._provider_name: str = "openai"
        self._provider_cfg: dict[str, Any] = {}
        self._model: str = "gpt-4o-mini"
        self._template: str = _DEFAULT_TEMPLATE

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "pointwise")
        if "rubric" in cfg:
            self._rubric = cfg["rubric"]
        elif "rubric_file" in cfg:
            self._rubric = Path(cfg["rubric_file"]).read_text().strip()
        else:
            raise ValueError("pointwise judge needs 'rubric' or 'rubric_file'")
        self._threshold = float(cfg.get("threshold", 4.0))
        # ``model`` accepts "openai:gpt-4o" or "gpt-4o" (default provider openai)
        model_str = cfg.get("model", "openai:gpt-4o-mini")
        if ":" in model_str:
            self._provider_name, self._model = model_str.split(":", 1)
        else:
            self._provider_name, self._model = "openai", model_str
        self._provider_cfg = cfg.get("provider_config", {})
        self._template = cfg.get("prompt_template", _DEFAULT_TEMPLATE)

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        provider = load_provider(self._provider_name, self._provider_cfg)
        expected_block = (
            _EXPECTED_BLOCK.format(expected=_render_input(ctx.expected))
            if ctx.expected is not None
            else ""
        )
        prompt = self._template.format(
            rubric=self._rubric,
            input=_render_input(ctx.input),
            output=ctx.output,
            expected_block=expected_block,
        )
        result = await provider.complete(prompt, model=self._model)
        score, reason = _parse_score_json(result.output)
        passed = score >= self._threshold
        raw = {
            "judge_model": f"{self._provider_name}:{self._model}",
            "judge_cost_usd": result.cost_usd,
            "score": score,
            "reason": reason,
            "raw": result.raw,
        }
        return [Score(self.id, self.kind, self.layer, float(score), passed, raw)]


class MockPointwiseJudge:
    """Deterministic offline judge for examples and tests."""

    kind = "judge"
    layer = 3

    def __init__(self) -> None:
        self.id: str = "mock_pointwise"
        self._score: float = 4.5
        self._threshold: float = 4.0
        self._noise_for_row: dict[str, float] = {}

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "mock_pointwise")
        self._score = float(cfg.get("score", 4.5))
        self._threshold = float(cfg.get("threshold", 4.0))
        # Optional per-row overrides keyed by row_id, useful for testing
        # regression detection without hitting an API.
        self._noise_for_row = {k: float(v) for k, v in (cfg.get("row_scores") or {}).items()}

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        s = self._noise_for_row.get(ctx.row_id, self._score)
        passed = s >= self._threshold
        return [
            Score(
                self.id,
                self.kind,
                self.layer,
                s,
                passed,
                {"reason": "mock judge", "threshold": self._threshold},
            )
        ]


_SCORE_RE = re.compile(r'"score"\s*:\s*([0-9]+(?:\.[0-9]+)?)')


def _parse_score_json(text: str) -> tuple[float, str]:
    """Extract ``score`` and ``reason`` from a judge's JSON-ish output."""
    try:
        obj = json.loads(text)
        return float(obj.get("score", 0)), str(obj.get("reason", ""))
    except json.JSONDecodeError:
        m = _SCORE_RE.search(text)
        if m:
            return float(m.group(1)), text.strip()[:200]
        return 0.0, f"unparseable judge output: {text[:120]}"
