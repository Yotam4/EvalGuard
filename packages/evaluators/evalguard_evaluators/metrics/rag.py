"""Layer-2 RAG metrics — deterministic n-gram proxies for RAGAS.

The four classic RAGAS axes (faithfulness, answer_relevancy,
context_precision, context_recall) are normally computed with an LLM
plus an embedding model. EvalGuard's pyramid puts those in Layer 3
(``judge_offline``). This module instead ships **deterministic**
proxies suitable for Layer 2: cheap, offline, no API keys required,
green in CI without a network.

The proxies are token-overlap based (n-gram intersection over union)
with light English-stopword filtering. They correlate well enough with
the LLM-based metrics for regression-detection in CI; production
deployments that need higher fidelity can register an LLM-backed
plugin under the ``evalguard.evaluators`` entry-point group with the
same evaluator id and override per-project.

Dataset row schema RAG metrics expect (read from ``ctx.extra``):

    {
      "id":               "...",
      "question":         "What is X?",
      "contexts":         ["passage 1", "passage 2", ...],
      "expected_answer":  "X is ..."
    }

``ctx.input`` and ``ctx.expected`` may be left empty for RAG runs;
metrics fall back to ``ctx.extra['question']`` and
``ctx.extra['expected_answer']`` respectively, so the same dataset
file works whether the user uses ``input``/``expected`` or
``question``/``expected_answer`` as primary keys.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from evalguard_evaluators.base import EvalContext, Score


# Minimal English stopword list. Keep it tiny — over-aggressive
# stopword stripping makes short answers look spuriously good.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "and", "or", "but", "if", "than", "that", "this", "these", "those",
    "it", "its", "i", "you", "he", "she", "we", "they", "them", "us",
    "do", "does", "did", "have", "has", "had", "will", "would", "can",
    "could", "should", "may", "might", "shall", "what", "who", "when",
    "where", "why", "how",
})

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _tokens(text: str) -> list[str]:
    """Lowercased word tokens with a tiny stopword filter applied."""
    if not text:
        return []
    return [t for t in (m.group(0).lower() for m in _WORD_RE.finditer(text))
            if t not in _STOPWORDS]


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    toks = _tokens(text)
    if n <= 1:
        return {(t,) for t in toks}
    return {tuple(toks[i:i + n]) for i in range(0, max(0, len(toks) - n + 1))}


def _coverage(reference: Iterable[str], target_text: str) -> float:
    """Fraction of reference tokens that also appear in ``target_text``.

    Returns 1.0 when reference is empty (vacuously satisfied — there's
    nothing to cover) so empty-question rows don't tank the metric.
    """
    ref = set(reference)
    if not ref:
        return 1.0
    target = set(_tokens(target_text))
    return len(ref & target) / len(ref)


def _resolve_question(ctx: EvalContext) -> str:
    q = ctx.extra.get("question") if isinstance(ctx.extra, dict) else None
    if isinstance(q, str) and q:
        return q
    return str(ctx.input) if ctx.input else ""


def _resolve_contexts(ctx: EvalContext) -> list[str]:
    contexts = ctx.extra.get("contexts") if isinstance(ctx.extra, dict) else None
    if not isinstance(contexts, (list, tuple)):
        return []
    return [str(c) for c in contexts if c is not None]


def _resolve_expected_answer(ctx: EvalContext) -> str:
    ea = ctx.extra.get("expected_answer") if isinstance(ctx.extra, dict) else None
    if isinstance(ea, str) and ea:
        return ea
    return str(ctx.expected) if ctx.expected else ""


# ---------------------------------------------------------------------------
# Common base


class _RagMetric:
    """Mixin: shared configure() / threshold / pass_below behaviour.

    Each subclass sets ``id`` / ``layer`` and implements ``_score``.
    """

    kind = "metric"
    layer = 2

    def __init__(self) -> None:
        self.id: str = self._default_id
        self._threshold: float = self._default_threshold

    # Subclass must override.
    _default_id: str = "rag_metric"
    _default_threshold: float = 0.5

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", self._default_id)
        self._threshold = float(cfg.get("threshold", self._default_threshold))

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        value, raw = self._score(ctx)
        passed = value >= self._threshold
        raw_full = {"threshold": self._threshold, **raw}
        return [Score(self.id, self.kind, self.layer, value, passed, raw_full)]

    # Subclass override.
    def _score(self, ctx: EvalContext) -> tuple[float, dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Faithfulness — output grounded in contexts


class FaithfulnessMetric(_RagMetric):
    """Fraction of output tokens covered by the union of contexts.

    A high score means the model didn't hallucinate beyond what the
    retrieved contexts say. Low score = output contains claims absent
    from the contexts (potential hallucination)."""

    _default_id = "faithfulness"
    _default_threshold = 0.5

    def _score(self, ctx: EvalContext) -> tuple[float, dict[str, Any]]:
        contexts = _resolve_contexts(ctx)
        if not contexts:
            return 1.0, {"reason": "no contexts; vacuously faithful", "n_contexts": 0}
        joined_contexts = " ".join(contexts)
        output_tokens = _tokens(ctx.output)
        if not output_tokens:
            return 1.0, {"reason": "empty output", "n_contexts": len(contexts)}
        value = _coverage(output_tokens, joined_contexts)
        return value, {
            "n_contexts":       len(contexts),
            "n_output_tokens":  len(output_tokens),
        }


# ---------------------------------------------------------------------------
# 2. Answer relevancy — output addresses the question


class AnswerRelevancyMetric(_RagMetric):
    """Fraction of question tokens reflected in the output.

    A high score means the output engaged with the topic of the
    question; low means the model went off-topic."""

    _default_id = "answer_relevancy"
    _default_threshold = 0.3

    def _score(self, ctx: EvalContext) -> tuple[float, dict[str, Any]]:
        question = _resolve_question(ctx)
        question_tokens = _tokens(question)
        if not question_tokens:
            return 1.0, {"reason": "empty question; vacuously relevant"}
        if not ctx.output:
            return 0.0, {"reason": "empty output"}
        value = _coverage(question_tokens, ctx.output)
        return value, {
            "n_question_tokens": len(question_tokens),
        }


# ---------------------------------------------------------------------------
# 3. Context precision — top-k contexts are relevant


class ContextPrecisionMetric(_RagMetric):
    """Fraction of provided contexts that share substantive vocabulary
    with the expected answer.

    A high score means the retriever's top-k weren't padded with
    irrelevant passages. Score = (#relevant_contexts) / (#contexts)."""

    _default_id = "context_precision"
    _default_threshold = 0.5

    def configure(self, cfg: dict[str, Any]) -> None:
        super().configure(cfg)
        # A context "counts" as relevant if its overlap with the
        # expected-answer token set exceeds this fraction. Default is
        # generous because retrieved passages are often loose-fit.
        self._relevance_min = float(cfg.get("relevance_min", 0.10))

    def _score(self, ctx: EvalContext) -> tuple[float, dict[str, Any]]:
        contexts = _resolve_contexts(ctx)
        expected = _resolve_expected_answer(ctx)
        ans_tokens = set(_tokens(expected))
        if not contexts:
            return 0.0, {"reason": "no contexts retrieved"}
        if not ans_tokens:
            # No reference to score precision against; vacuously satisfied.
            return 1.0, {"reason": "no expected_answer", "n_contexts": len(contexts)}
        relevant = 0
        per_context: list[float] = []
        for c in contexts:
            ctx_tokens = set(_tokens(c))
            overlap = (len(ans_tokens & ctx_tokens) / len(ans_tokens)) if ans_tokens else 0.0
            per_context.append(overlap)
            if overlap >= self._relevance_min:
                relevant += 1
        value = relevant / len(contexts)
        return value, {
            "n_contexts":      len(contexts),
            "n_relevant":      relevant,
            "relevance_min":   self._relevance_min,
            "per_context":     per_context,
        }


# ---------------------------------------------------------------------------
# 4. Context recall — relevant info is present in retrieved contexts


class ContextRecallMetric(_RagMetric):
    """Fraction of expected-answer tokens covered by the union of
    contexts.

    A high score means the retriever surfaced enough material for the
    answer to be reconstructable. Low score means even with a perfect
    generator the system would fail (retriever gap, not generator gap)."""

    _default_id = "context_recall"
    _default_threshold = 0.5

    def _score(self, ctx: EvalContext) -> tuple[float, dict[str, Any]]:
        contexts = _resolve_contexts(ctx)
        expected = _resolve_expected_answer(ctx)
        ans_tokens = _tokens(expected)
        if not ans_tokens:
            return 1.0, {"reason": "no expected_answer; vacuous", "n_contexts": len(contexts)}
        if not contexts:
            return 0.0, {"reason": "no contexts retrieved"}
        joined = " ".join(contexts)
        value = _coverage(ans_tokens, joined)
        return value, {
            "n_contexts":         len(contexts),
            "n_expected_tokens":  len(ans_tokens),
        }
