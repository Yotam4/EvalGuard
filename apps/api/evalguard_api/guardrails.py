"""Inline (layer-4) gate enforcement for the /invoke path.

The CLI's batch gate evaluator in ``evalguard_cli.local.gate.evaluate_gates``
expects aggregated metrics (``by_evaluator`` / ``by_layer`` / ``by_tag``
rollups built from a finished run) and produces one verdict per gate
config.  That shape is wrong for inline dispatch: ``/invoke`` has
exactly one row, no rollups, and needs the verdict *before* the
response is built.  This module is the inline counterpart — it
inspects the layer-4 ``Score`` objects emitted by guardrail
evaluators against the layer's gate config and returns a single
``InlineVerdict`` the proxy can act on.

Why not extend the batch evaluator with a degenerate single-row
mode?  Threshold semantics drift: the batch evaluator computes
``pass_rate`` over a population (e.g. "fail the gate if pass rate
< 0.95"), which for n=1 collapses to "fail if this row didn't
pass".  That's the right behaviour for a guardrail, but expressing
it through the batch primitive requires synthesising
``by_layer[4] = {pass_rate: 0/1}`` on the fly — more work than the
~25-line single-row variant below, and harder to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

from evalguard_evaluators.base import Score


_MODE_VALUES: tuple[str, ...] = ("block", "warn", "log")
_GUARDRAIL_LAYER: int = 4


@dataclass(frozen=True)
class InlineVerdict:
    """Decision an inline layer gate produced for one row.

    - ``allow``: True when no guardrail score failed (or when the gate
      is configured ``mode=log`` and we never block).
    - ``mode``: the layer's dispatch mode (block / warn / log) carried
      through so the /invoke handler can decide whether to refuse,
      annotate, or just record.
    - ``failed_scores``: the guardrail scores that triggered the
      verdict.  Empty when the verdict is an allow.
    - ``reason``: short human-readable string the proxy can echo into
      the refusal body / audit event payload.
    """

    allow: bool
    mode: Literal["block", "warn", "log"]
    failed_scores: tuple[Score, ...]
    reason: str | None
    layer_gate_id: str | None


def evaluate_inline_gate(
    guardrail_scores: Iterable[Score],
    layer_gate_cfg: dict[str, Any] | None,
    *,
    layer_gate_id: str | None = "judge_online",
) -> InlineVerdict:
    """Apply a single row's layer-4 scores to the layer-gate config.

    ``layer_gate_cfg`` is the parsed ``layers.judge_online`` block
    from the project YAML (or ``None`` when the operator hasn't
    configured one — in which case any guardrail failure defaults to
    ``mode=block``, matching the safety bias an inline guardrail
    layer carries).

    A score is considered failing when ``passed=False``.  When the
    layer config also sets a ``threshold.min`` value, scores with
    ``value < threshold.min`` are also marked failing (this is the
    seam Llama-Guard-style continuous scorers plug into — they emit
    ``value in [0, 1]`` and the gate decides the cutoff).

    The verdict's ``mode`` field reflects the layer's configured
    dispatch policy; when ``mode`` is unset the function falls back
    to ``severity`` (so legacy ``severity: block`` configs still
    drive inline behaviour during the L4 rollout) and finally to
    ``"block"`` (safe default for the guardrail layer).
    """
    scores = tuple(s for s in guardrail_scores if s.layer == _GUARDRAIL_LAYER)
    mode = _resolve_mode(layer_gate_cfg)
    threshold_min = _threshold_min(layer_gate_cfg)

    failed = tuple(
        s for s in scores
        if (not s.passed)
        or (threshold_min is not None and s.value < threshold_min)
    )
    if not failed:
        return InlineVerdict(
            allow=True, mode=mode, failed_scores=(),
            reason=None, layer_gate_id=layer_gate_id,
        )

    # On a failing score the verdict's allow flag follows the mode:
    # ``block`` refuses, ``warn`` and ``log`` let the call through
    # (they only annotate / record).  The /invoke handler reads both
    # ``allow`` and ``mode`` so it can distinguish "let through and
    # mark failed" (warn) from "let through silently" (log).
    return InlineVerdict(
        allow=(mode != "block"),
        mode=mode,
        failed_scores=failed,
        reason=_format_reason(failed, threshold_min),
        layer_gate_id=layer_gate_id,
    )


def _resolve_mode(cfg: dict[str, Any] | None) -> Literal["block", "warn", "log"]:
    if cfg is None:
        return "block"
    raw = cfg.get("mode") or cfg.get("severity") or "block"
    if raw not in _MODE_VALUES:
        return "block"
    return raw  # type: ignore[return-value]


def _threshold_min(cfg: dict[str, Any] | None) -> float | None:
    if cfg is None:
        return None
    thr = cfg.get("threshold")
    if not isinstance(thr, dict):
        return None
    raw = thr.get("min")
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _format_reason(
    failed: tuple[Score, ...],
    threshold_min: float | None,
) -> str:
    """Render a short, audit-friendly reason string.

    Capped at one evaluator id + the threshold context so a misbehaving
    evaluator that emits huge ``raw`` blobs can't blow up the audit
    event payload.  The full failing scores live on the row's
    ``detail_json.scores`` for drill-down.
    """
    first = failed[0]
    if threshold_min is not None and first.value < threshold_min:
        return (
            f"guardrail {first.evaluator_id!r} scored {first.value:.3f} "
            f"< threshold {threshold_min:.3f}"
        )
    return f"guardrail {first.evaluator_id!r} refused (passed=False)"
