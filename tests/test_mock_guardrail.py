"""Unit tests for the ``MockGuardrail`` layer-4 evaluator."""

from __future__ import annotations

import asyncio

import pytest

from evalguard_evaluators.base import EvalContext
from evalguard_evaluators.guardrails.mock import MockGuardrail


def _ctx(output: str) -> EvalContext:
    return EvalContext(
        row_id="r1", input="ignored", expected=None,
        output=output, provider="mock", model="m", extra={},
    )


def test_mock_guardrail_passes_when_substring_absent() -> None:
    g = MockGuardrail()
    g.configure({"forbidden": "secret"})
    scores = asyncio.run(g.evaluate(_ctx("the answer is 42")))
    assert len(scores) == 1
    s = scores[0]
    assert s.passed is True
    assert s.value == 1.0
    assert s.layer == 4
    assert s.evaluator_kind == "guardrail"


def test_mock_guardrail_fails_when_substring_present() -> None:
    g = MockGuardrail()
    g.configure({"forbidden": "secret"})
    scores = asyncio.run(g.evaluate(_ctx("here is the secret key")))
    assert len(scores) == 1
    assert scores[0].passed is False
    assert scores[0].value == 0.0
    assert scores[0].raw["matched"] is True


def test_mock_guardrail_case_insensitive_by_default() -> None:
    g = MockGuardrail()
    g.configure({"forbidden": "SECRET"})
    scores = asyncio.run(g.evaluate(_ctx("contains secret in lowercase")))
    assert scores[0].passed is False


def test_mock_guardrail_case_sensitive_when_requested() -> None:
    g = MockGuardrail()
    g.configure({"forbidden": "Secret", "case_sensitive": True})
    # Wrong case → no match → pass.
    scores = asyncio.run(g.evaluate(_ctx("contains secret in lowercase")))
    assert scores[0].passed is True
    # Right case → match → fail.
    scores = asyncio.run(g.evaluate(_ctx("contains Secret here")))
    assert scores[0].passed is False


def test_mock_guardrail_rejects_empty_forbidden() -> None:
    g = MockGuardrail()
    with pytest.raises(ValueError, match="forbidden"):
        g.configure({"forbidden": ""})
    with pytest.raises(ValueError, match="forbidden"):
        g.configure({})


def test_mock_guardrail_respects_id_override() -> None:
    g = MockGuardrail()
    g.configure({"id": "policy-1", "forbidden": "x"})
    scores = asyncio.run(g.evaluate(_ctx("no match")))
    assert scores[0].evaluator_id == "policy-1"


def test_mock_guardrail_registers_under_evalguard_entry_points() -> None:
    """Drift canary: the entry-point convention is the wire contract
    between project YAML and the evaluator package. A rename here
    would silently change which evaluator a config targets."""
    from evalguard_evaluators.registry import (
        iter_evaluators, reset_registry_cache,
    )
    reset_registry_cache()
    ep_map = iter_evaluators()
    assert "guardrail.mock" in ep_map
    assert ep_map["guardrail.mock"] is MockGuardrail
