"""Unit tests for ``evalguard_api.guardrails.evaluate_inline_gate``.

This is the inline (single-row) gate evaluator used by ``/invoke``'s
layer-4 enforcement path.  Distinct from the batch-shaped
``evalguard_cli.local.gate.evaluate_gates`` which operates on
aggregated metrics from a finished run.
"""

from __future__ import annotations

from evalguard_api.guardrails import InlineVerdict, evaluate_inline_gate
from evalguard_evaluators.base import Score


def _score(passed: bool, value: float = 1.0, evaluator_id: str = "g1") -> Score:
    return Score(
        evaluator_id=evaluator_id, evaluator_kind="guardrail",
        layer=4, value=value, passed=passed, raw={},
    )


# ---------------------------------------------------------------------------
# Allow paths


def test_no_scores_returns_allow_with_default_block_mode() -> None:
    v = evaluate_inline_gate([], {"mode": "block"})
    assert isinstance(v, InlineVerdict)
    assert v.allow is True
    assert v.mode == "block"
    assert v.failed_scores == ()
    assert v.reason is None


def test_all_passing_scores_returns_allow() -> None:
    scores = [_score(passed=True), _score(passed=True, evaluator_id="g2")]
    v = evaluate_inline_gate(scores, {"mode": "block"})
    assert v.allow is True
    assert v.failed_scores == ()


def test_non_guardrail_layer_scores_are_ignored() -> None:
    """Only layer=4 scores feed the inline verdict — L1–L3 scores
    belong to the batch gate path."""
    l1_fail = Score(
        evaluator_id="h", evaluator_kind="heuristic", layer=1,
        value=0.0, passed=False, raw={},
    )
    v = evaluate_inline_gate([l1_fail], {"mode": "block"})
    assert v.allow is True


# ---------------------------------------------------------------------------
# Block / warn / log behaviour


def test_failing_score_with_block_mode_refuses() -> None:
    v = evaluate_inline_gate([_score(passed=False, value=0.0)], {"mode": "block"})
    assert v.allow is False
    assert v.mode == "block"
    assert len(v.failed_scores) == 1
    assert v.reason is not None


def test_failing_score_with_warn_mode_allows_but_annotates() -> None:
    """warn lets the request through but the failed scores are still
    surfaced — the /invoke handler marks the row passed=False."""
    v = evaluate_inline_gate([_score(passed=False)], {"mode": "warn"})
    assert v.allow is True
    assert v.mode == "warn"
    assert len(v.failed_scores) == 1


def test_failing_score_with_log_mode_allows_silently() -> None:
    v = evaluate_inline_gate([_score(passed=False)], {"mode": "log"})
    assert v.allow is True
    assert v.mode == "log"
    assert len(v.failed_scores) == 1


def test_mode_falls_back_to_severity_then_block() -> None:
    """Operators migrating from batch ``severity`` to inline ``mode``
    shouldn't have their config silently downgraded mid-rollout."""
    v = evaluate_inline_gate([_score(passed=False)], {"severity": "warn"})
    assert v.mode == "warn"
    # No mode + no severity → safe default for an inline guardrail.
    v = evaluate_inline_gate([_score(passed=False)], {})
    assert v.mode == "block"
    # No layer gate config at all → also block.
    v = evaluate_inline_gate([_score(passed=False)], None)
    assert v.mode == "block"


# ---------------------------------------------------------------------------
# Threshold semantics


def test_threshold_min_below_blocks_even_when_passed_true() -> None:
    """Continuous scorers (Llama Guard) emit value ∈ [0,1] with
    passed=True; the layer's ``threshold.min`` is the operator's
    cutoff."""
    s = _score(passed=True, value=0.3)
    v = evaluate_inline_gate(
        [s], {"mode": "block", "threshold": {"min": 0.5}},
    )
    assert v.allow is False
    assert v.failed_scores[0].value == 0.3
    assert "0.300" in (v.reason or "")
    assert "0.500" in (v.reason or "")


def test_threshold_min_above_allows() -> None:
    s = _score(passed=True, value=0.9)
    v = evaluate_inline_gate(
        [s], {"mode": "block", "threshold": {"min": 0.5}},
    )
    assert v.allow is True


def test_no_threshold_uses_passed_only() -> None:
    s = _score(passed=True, value=0.1)  # passed=True wins without a threshold
    v = evaluate_inline_gate([s], {"mode": "block"})
    assert v.allow is True
