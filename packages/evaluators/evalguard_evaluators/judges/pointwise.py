"""Layer-3 LLM-as-judge: G-Eval-style pointwise rubric scoring.

Two implementations:
- ``PointwiseJudge`` calls a real provider (OpenAI by default). Its own
  LLM call is recorded as a nested ``provider.called`` audit event when
  the executor has set an ``_audit_hook`` on the instance.
- ``MockPointwiseJudge`` returns a deterministic, configurable score so
  examples and tests can run offline. This is what the quickstart uses.

Both judges share a base class that carries the optional audit hook so
the executor can treat them uniformly.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from evalguard_evaluators.base import EvalContext, Score
from evalguard_evaluators.registry import load_provider


class _AuditableJudge:
    """Mixin: judges look up an audit hook via a task-local lookup.

    The executor stores the hook on a ``contextvars.ContextVar`` before
    each call so concurrent rows running on the *same* judge instance
    don't trample each other (instance-attribute storage was racy).

    Plugins are insulated from the lookup mechanism — they can either
    read ``self._audit_hook`` (which delegates to the lookup) or import
    ``current_audit_hook`` directly.
    """

    @property
    def _audit_hook(self) -> Any:
        # Imported lazily to avoid a hard dep from evaluators → cli;
        # the cli package owns the lookup, evaluators just read it.
        try:
            from evalguard_cli.local.audit import current_audit_hook
        except ImportError:
            return None
        return current_audit_hook()

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


class PointwiseJudge(_AuditableJudge):
    kind = "judge"
    layer = 3

    def __init__(self) -> None:
        self.id: str = "pointwise"
        self._rubric: str = ""
        self._threshold: float = 4.0
        self._provider_name: str = "openai"
        self._provider_cfg: dict[str, Any] = {}
        self._params: dict[str, Any] = {}
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
        self._params = cfg.get("params", {})
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

        t0 = time.monotonic()
        result = await provider.complete(prompt, model=self._model, params=self._params)
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Nested audit event for the judge's own LLM call. ``parent_span_id``
        # is the evaluator-event's span (set up by the executor before this
        # call), so a downstream UI can render the judge call as a child of
        # the evaluator.judge.invoked event in the trace tree.
        if self._audit_hook is not None:
            tokens = _extract_tokens(result.raw)
            self._audit_hook.emit_provider_call(
                provider=self._provider_name,
                model=self._model,
                prompt=prompt,
                response=result.output,
                model_params=self._params,
                tokens=tokens,
                cost_usd=result.cost_usd,
                latency_ms=latency_ms,
                raw=result.raw,
                is_judge_call=True,
            )

        score, reason = _parse_score_json(result.output)
        passed = score >= self._threshold
        raw = {
            "judge_model": f"{self._provider_name}:{self._model}",
            "judge_cost_usd": result.cost_usd,
            "judge_latency_ms": latency_ms,
            "score": score,
            "reason": reason,
            "raw": result.raw,
        }
        return [Score(self.id, self.kind, self.layer, float(score), passed, raw)]


class MockPointwiseJudge(_AuditableJudge):
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


def _extract_tokens(raw: dict[str, Any] | None) -> dict[str, int] | None:
    """Best-effort token extraction from common provider raw shapes.

    Mirrors the helper in ``local_executor`` so judges produce the same
    ``tokens`` field shape on their nested ``provider.called`` events.
    """
    if not raw:
        return None
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return None
    return {
        "prompt":     int(usage.get("prompt_tokens", 0) or 0),
        "completion": int(usage.get("completion_tokens", 0) or 0),
        "total":      int(usage.get("total_tokens", 0) or 0),
    }


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
