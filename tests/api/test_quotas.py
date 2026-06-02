"""Direct unit coverage for the rate-limiter + cost-cap helpers.

Round-4 ultra-review (Agent-2 D): the helpers in
``evalguard_api.quotas`` had only integration-level coverage via
``test_invoke.py``.  These tests target the units directly so a
refactor that breaks one of them (window-edge handling, reset
correctness, GC) surfaces immediately.
"""

from __future__ import annotations

import time

import pytest

from evalguard_api.quotas import (
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    _LIMITER_GC_THRESHOLD,
    _LIMITER_STATE,
    _WINDOW_S,
    rate_limit_check,
    reset_rate_limiter,
    todays_live_run_cost,
)


@pytest.fixture(autouse=True)
def _isolate_limiter():
    """Drop module state before AND after each test so the order
    of cases doesn't matter."""
    reset_rate_limiter()
    yield
    reset_rate_limiter()


# ---------------------------------------------------------------------------
# rate_limit_check


def test_rate_limit_zero_disables_check():
    """``limit_per_minute=0`` is the documented disable path; every
    call must be allowed with retry_after=0."""
    for _ in range(1000):
        allowed, retry = rate_limit_check("k", limit_per_minute=0)
        assert allowed is True
        assert retry == 0


def test_rate_limit_negative_disables_check():
    """A negative value should also disable (defence-in-depth — the
    push-config validator now rejects negatives, but the runtime
    check must still be safe if one slips through)."""
    allowed, _ = rate_limit_check("k", limit_per_minute=-5)
    assert allowed is True


def test_rate_limit_just_at_cap_allowed():
    """The Nth call within the window is allowed; the (N+1)th is not.
    Inclusive-low boundary: ``len(history) < limit`` is the allow
    predicate, ``>=`` is the reject."""
    for i in range(3):
        allowed, _ = rate_limit_check("k", limit_per_minute=3)
        assert allowed is True, f"call {i} should be allowed"
    allowed, retry = rate_limit_check("k", limit_per_minute=3)
    assert allowed is False
    assert retry >= 1


def test_rate_limit_window_aging(monkeypatch):
    """After ``_WINDOW_S`` elapses, the oldest timestamp ages out
    and a new request becomes allowed."""
    # Patch ``time.monotonic`` so the test runs in milliseconds.
    base = 1000.0
    now = [base]
    monkeypatch.setattr("evalguard_api.quotas.time.monotonic", lambda: now[0])

    # Fill the window with 2 requests at t=0.
    for _ in range(2):
        allowed, _ = rate_limit_check("k", limit_per_minute=2)
        assert allowed
    # 3rd at t=0 → rejected.
    allowed, _ = rate_limit_check("k", limit_per_minute=2)
    assert allowed is False

    # Advance well past _WINDOW_S so both prior entries age out.
    now[0] = base + _WINDOW_S + 1.0
    allowed, _ = rate_limit_check("k", limit_per_minute=2)
    assert allowed is True


def test_rate_limit_retry_after_reflects_oldest(monkeypatch):
    """Retry-After is the time until the oldest in-window timestamp
    ages out, not a hardcoded value."""
    base = 1000.0
    now = [base]
    monkeypatch.setattr("evalguard_api.quotas.time.monotonic", lambda: now[0])

    # Drop two timestamps; the oldest is at base.
    rate_limit_check("k", limit_per_minute=2)
    rate_limit_check("k", limit_per_minute=2)

    # Try again 10s in — oldest is 10s old, so retry-after is
    # _WINDOW_S - 10 = 50 (give or take rounding).
    now[0] = base + 10.0
    allowed, retry = rate_limit_check("k", limit_per_minute=2)
    assert allowed is False
    assert 49 <= retry <= 51, retry


def test_rate_limit_keys_are_independent():
    """Two distinct keys have independent counters."""
    for _ in range(3):
        allowed, _ = rate_limit_check("alice", limit_per_minute=3)
        assert allowed
    # Alice is now at cap.
    allowed, _ = rate_limit_check("alice", limit_per_minute=3)
    assert allowed is False
    # Bob is unaffected.
    allowed, _ = rate_limit_check("bob", limit_per_minute=3)
    assert allowed is True


def test_reset_rate_limiter_clears_state():
    """After reset, a previously-capped key is allowed again."""
    for _ in range(3):
        rate_limit_check("k", limit_per_minute=3)
    allowed, _ = rate_limit_check("k", limit_per_minute=3)
    assert allowed is False   # at cap
    reset_rate_limiter()
    allowed, _ = rate_limit_check("k", limit_per_minute=3)
    assert allowed is True


def test_limiter_state_evicts_empties_past_threshold(monkeypatch):
    """Round-4 ultra-review (Agent-1 C): the dict shouldn't grow
    unbounded with one permanent entry per distinct key seen.  After
    aging out the window, the eviction sweep drops empty deques."""
    base = 1000.0
    now = [base]
    monkeypatch.setattr("evalguard_api.quotas.time.monotonic", lambda: now[0])

    # Seed the dict with > _LIMITER_GC_THRESHOLD distinct keys.
    for i in range(_LIMITER_GC_THRESHOLD + 5):
        rate_limit_check(f"k_{i}", limit_per_minute=10)
    pre_eviction = len(_LIMITER_STATE)
    assert pre_eviction >= _LIMITER_GC_THRESHOLD

    # Advance past the window so every entry's deque drains on its
    # next visit.  Touch ONE key, which triggers the prune-then-GC
    # path.
    now[0] = base + _WINDOW_S + 1.0
    rate_limit_check("trigger", limit_per_minute=10)

    # The trigger call's own entry stays; the old empties should be
    # dropped.  Allow a generous margin because the GC only fires
    # when len(_LIMITER_STATE) > threshold AT INSERT time, so the
    # exact post-count depends on the order — but it must be much
    # smaller than the seeded count.
    post_eviction = len(_LIMITER_STATE)
    assert post_eviction < pre_eviction, (
        f"GC didn't shrink the dict: pre={pre_eviction}, post={post_eviction}"
    )


# ---------------------------------------------------------------------------
# todays_live_run_cost


def test_todays_live_run_cost_returns_zero_when_no_run_exists(client):
    """First call of the day path: no live run yet → 0.0 (NOT NaN /
    None / KeyError)."""
    engine = client.app.state.engine
    with engine.begin() as conn:
        # Use the default project from the lifespan-bootstrapped admin.
        from sqlalchemy import text
        proj = conn.execute(
            text("SELECT project_id FROM projects WHERE slug='default'"),
        ).scalar_one()
        cost = todays_live_run_cost(conn, proj)
    assert cost == 0.0


def test_default_rate_limit_constant_is_sane():
    """Lock the default so a change is loud."""
    assert DEFAULT_RATE_LIMIT_PER_MINUTE == 60
