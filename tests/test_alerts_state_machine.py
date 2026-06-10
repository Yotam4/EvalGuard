"""Pure-logic tests for the alerting state machine.

These tests pass a constructed clock; no DB, no Redis, no time.time.
Determinism is the whole point — the engine has to behave the same
way at noon as at midnight, and CI shouldn't get flaky around
suppress windows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from evalguard_api.alerts import (
    evaluate_threshold, parse_window, state_transition,
)


_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# parse_window


def test_parse_window_minutes() -> None:
    assert parse_window("5m") == timedelta(minutes=5)
    assert parse_window("15m") == timedelta(minutes=15)


def test_parse_window_hours_and_days() -> None:
    assert parse_window("1h") == timedelta(hours=1)
    assert parse_window("24h") == timedelta(hours=24)
    assert parse_window("7d") == timedelta(days=7)


def test_parse_window_rejects_zero_and_garbage() -> None:
    with pytest.raises(ValueError):
        parse_window("0m")
    with pytest.raises(ValueError):
        parse_window("five minutes")
    with pytest.raises(ValueError):
        parse_window("1w")  # weeks not in the spec


# ---------------------------------------------------------------------------
# evaluate_threshold


def test_threshold_min_below_fails() -> None:
    assert evaluate_threshold(0.85, {"min": 0.9}) is False


def test_threshold_min_at_or_above_passes() -> None:
    assert evaluate_threshold(0.9, {"min": 0.9}) is True
    assert evaluate_threshold(0.95, {"min": 0.9}) is True


def test_threshold_max_handled_symmetrically() -> None:
    # max is for "fail when observed value goes ABOVE this" — e.g.
    # latency budgets.
    assert evaluate_threshold(2000, {"max": 1500}) is False
    assert evaluate_threshold(1500, {"max": 1500}) is True


def test_threshold_band_both_sides() -> None:
    band = {"min": 0.8, "max": 0.99}
    assert evaluate_threshold(0.9, band) is True
    assert evaluate_threshold(0.7, band) is False
    assert evaluate_threshold(1.0, band) is False


def test_threshold_none_observed_passes_vacuously() -> None:
    """An empty window pretends the gate passed — the engine relies
    on ``min_samples`` to catch the quiet-period case before the
    threshold runs."""
    assert evaluate_threshold(None, {"min": 0.9}) is True


# ---------------------------------------------------------------------------
# state_transition


def test_pass_to_pass_is_no_change() -> None:
    d = state_transition("pass", True, None, _NOW, suppress_secs=600)
    assert d.new_state == "pass"
    assert d.transition == "no_change"
    assert d.fire is False
    assert d.resolve is False


def test_pass_to_fail_fires() -> None:
    d = state_transition("pass", False, None, _NOW, suppress_secs=600)
    assert d.new_state == "fail"
    assert d.transition == "pass_to_fail"
    assert d.fire is True


def test_fail_to_pass_resolves() -> None:
    d = state_transition(
        "fail", True, _NOW - timedelta(minutes=5), _NOW, suppress_secs=600,
    )
    assert d.new_state == "pass"
    assert d.transition == "fail_to_pass"
    assert d.resolve is True
    assert d.fire is False


def test_fail_within_suppress_window_is_suppressed() -> None:
    """Same fail re-detected 60s after first fire while suppress is
    600s: state moves to 'suppressed', no notifier fires."""
    d = state_transition(
        "fail", False, _NOW - timedelta(seconds=60), _NOW, suppress_secs=600,
    )
    assert d.new_state == "suppressed"
    assert d.transition == "fail_to_suppressed"
    assert d.fire is False
    assert d.suppress is True


def test_fail_after_suppress_window_refires() -> None:
    """Re-detected 700s after first fire when suppress is 600s:
    suppression has elapsed → re-fire notifier."""
    d = state_transition(
        "fail", False, _NOW - timedelta(seconds=700), _NOW, suppress_secs=600,
    )
    assert d.new_state == "fail"
    assert d.fire is True


def test_suppressed_to_pass_resolves() -> None:
    """When a suppressed alert recovers, we still emit a resolve
    event so the operator sees the closure."""
    d = state_transition(
        "suppressed", True, _NOW - timedelta(minutes=5), _NOW, suppress_secs=600,
    )
    assert d.new_state == "pass"
    assert d.transition == "suppressed_to_pass"
    assert d.resolve is True


def test_suppress_disabled_when_secs_is_zero() -> None:
    """``suppress_secs: 0`` ⇒ every fail check fires a new notifier."""
    d = state_transition(
        "fail", False, _NOW - timedelta(seconds=1), _NOW, suppress_secs=0,
    )
    assert d.new_state == "fail"
    assert d.fire is True
