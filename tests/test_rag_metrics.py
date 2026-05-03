"""Layer-2 RAG metric proxies (faithfulness / answer_relevancy /
context_precision / context_recall).

The implementations are deterministic n-gram proxies. These tests pin
the qualitative behaviour: a faithful answer scores high, a
hallucinated one scores low, etc. They are NOT calibrated against
ground-truth RAGAS — proxies are wedge-of-the-truth, not the truth.
"""

from __future__ import annotations

import asyncio

from evalguard_evaluators.base import EvalContext
from evalguard_evaluators.metrics.rag import (
    AnswerRelevancyMetric,
    ContextPrecisionMetric,
    ContextRecallMetric,
    FaithfulnessMetric,
)


def _ctx(*, output: str, question: str = "", contexts: list[str] | None = None,
         expected_answer: str = "") -> EvalContext:
    return EvalContext(
        row_id="r", input=question, expected=expected_answer,
        output=output, provider="mock", model="m",
        extra={
            "question":        question,
            "contexts":        contexts or [],
            "expected_answer": expected_answer,
        },
    )


def _run(metric, ctx: EvalContext) -> float:
    [score] = asyncio.run(metric.evaluate(ctx))
    return score.value


# ---------------------------------------------------------------------------
# faithfulness


def test_faithfulness_high_when_output_grounded_in_contexts():
    m = FaithfulnessMetric(); m.configure({})
    ctx = _ctx(
        output="Paris is the capital of France.",
        contexts=["The capital of France is Paris."],
    )
    assert _run(m, ctx) >= 0.6


def test_faithfulness_low_when_output_introduces_unsupported_claims():
    m = FaithfulnessMetric(); m.configure({})
    ctx = _ctx(
        output="Wikipedia mentions zebras crossings frequently downtown.",
        contexts=["Paris is the capital of France."],
    )
    assert _run(m, ctx) <= 0.2


def test_faithfulness_vacuous_when_no_contexts():
    m = FaithfulnessMetric(); m.configure({})
    ctx = _ctx(output="anything", contexts=[])
    # No contexts → vacuously faithful (proxy can't disprove faithfulness).
    assert _run(m, ctx) == 1.0


# ---------------------------------------------------------------------------
# answer_relevancy


def test_answer_relevancy_high_when_output_addresses_question():
    m = AnswerRelevancyMetric(); m.configure({})
    ctx = _ctx(
        output="The capital of France is Paris, which sits on the Seine.",
        question="What is the capital of France?",
    )
    assert _run(m, ctx) >= 0.6


def test_answer_relevancy_low_when_off_topic():
    m = AnswerRelevancyMetric(); m.configure({})
    ctx = _ctx(
        output="Bananas grow on trees in tropical climates.",
        question="What is the capital of France?",
    )
    assert _run(m, ctx) <= 0.2


def test_answer_relevancy_zero_when_empty_output():
    m = AnswerRelevancyMetric(); m.configure({})
    ctx = _ctx(output="", question="What is the capital of France?")
    assert _run(m, ctx) == 0.0


# ---------------------------------------------------------------------------
# context_precision


def test_context_precision_one_when_all_contexts_relevant():
    m = ContextPrecisionMetric(); m.configure({})
    ctx = _ctx(
        output="ignored",
        expected_answer="Paris is the capital of France",
        contexts=[
            "Paris has been the capital of France for centuries.",
            "France's capital is Paris and it sits on the Seine.",
        ],
    )
    assert _run(m, ctx) == 1.0


def test_context_precision_low_when_padded_with_irrelevant():
    m = ContextPrecisionMetric(); m.configure({})
    ctx = _ctx(
        output="ignored",
        expected_answer="Paris is the capital of France",
        contexts=[
            "Paris has been the capital of France for centuries.",
            "Bananas grow on trees in tropical climates.",
            "Quantum chromodynamics describes the strong nuclear force.",
        ],
    )
    # 1 of 3 contexts is on-topic → precision ≈ 0.33
    val = _run(m, ctx)
    assert 0.20 <= val <= 0.50


# ---------------------------------------------------------------------------
# context_recall


def test_context_recall_high_when_answer_recoverable_from_contexts():
    m = ContextRecallMetric(); m.configure({})
    ctx = _ctx(
        output="ignored",
        expected_answer="Paris capital France",
        contexts=["The capital of France is Paris.", "France is in Europe."],
    )
    assert _run(m, ctx) == 1.0


def test_context_recall_zero_when_no_contexts():
    m = ContextRecallMetric(); m.configure({})
    ctx = _ctx(
        output="ignored",
        expected_answer="Paris capital France",
        contexts=[],
    )
    assert _run(m, ctx) == 0.0


def test_context_recall_low_when_answer_not_in_contexts():
    m = ContextRecallMetric(); m.configure({})
    ctx = _ctx(
        output="ignored",
        expected_answer="Paris capital France",
        contexts=["Bananas grow on trees in tropical climates."],
    )
    assert _run(m, ctx) <= 0.2


# ---------------------------------------------------------------------------
# Threshold + score-pass plumbing


def test_threshold_drives_passed_field():
    m = FaithfulnessMetric(); m.configure({"threshold": 0.99})
    ctx = _ctx(
        output="grounded paraphrase here",
        contexts=["the same grounded paraphrase here"],
    )
    [score] = asyncio.run(m.evaluate(ctx))
    # Even with high overlap, the 0.99 threshold should be hard to clear
    # for a one-sentence output. Verify the threshold drives passed.
    assert score.passed == (score.value >= 0.99)
    assert score.raw["threshold"] == 0.99


def test_metrics_resolve_from_ctx_input_and_expected_when_extra_missing():
    """Backward-compatibility: rows that use ``input`` / ``expected``
    instead of ``question`` / ``expected_answer`` still work."""
    m = AnswerRelevancyMetric(); m.configure({})
    ctx = EvalContext(
        row_id="r",
        input="What is the capital of France?",  # falls back here
        expected="Paris",
        output="The capital is Paris.",
        provider="mock", model="m",
    )
    assert _run(m, ctx) >= 0.5
