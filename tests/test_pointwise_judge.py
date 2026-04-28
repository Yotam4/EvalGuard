"""Pointwise judge JSON parsing and mock variant."""

from __future__ import annotations

import asyncio

from evalguard_evaluators.base import EvalContext
from evalguard_evaluators.judges.pointwise import (
    MockPointwiseJudge,
    _parse_score_json,
)


def test_parse_score_from_clean_json() -> None:
    score, reason = _parse_score_json('{"score": 4, "reason": "good"}')
    assert score == 4.0
    assert reason == "good"


def test_parse_score_falls_back_to_regex_when_not_pure_json() -> None:
    text = 'Some preamble. {"score": 3.5, ...} trailing text'
    score, reason = _parse_score_json(text)
    assert score == 3.5
    assert reason  # non-empty


def test_parse_score_returns_zero_for_unparseable() -> None:
    score, reason = _parse_score_json("totally unrelated reply")
    assert score == 0.0
    assert "unparseable" in reason


def test_mock_judge_uses_configured_score() -> None:
    j = MockPointwiseJudge()
    j.configure({"id": "q", "score": 4.5, "threshold": 4.0})
    ctx = EvalContext(row_id="r1", input="x", expected=None, output="y", provider="mock", model="m")
    scores = asyncio.run(j.evaluate(ctx))
    assert len(scores) == 1
    assert scores[0].value == 4.5
    assert scores[0].passed is True


def test_mock_judge_per_row_overrides_drive_failures() -> None:
    j = MockPointwiseJudge()
    j.configure({"id": "q", "score": 5.0, "threshold": 4.0,
                 "row_scores": {"bad": 1.0}})
    ctx_good = EvalContext(row_id="ok", input="", expected=None, output="", provider="m", model="m")
    ctx_bad = EvalContext(row_id="bad", input="", expected=None, output="", provider="m", model="m")
    assert asyncio.run(j.evaluate(ctx_good))[0].passed is True
    assert asyncio.run(j.evaluate(ctx_bad))[0].passed is False
