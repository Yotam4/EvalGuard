"""Per-key rate limiting + per-project cost caps for the /invoke proxy.

Round-4 review-pass big-ticket #7.  Without these the proxy is a
DoS / wallet-burn vector: a leaked bearer token can hammer the
endpoint at line rate, racking up upstream provider charges with no
server-side guard.

Two complementary surfaces:

- ``rate_limit_check(key_id, limit_per_minute)`` — sliding-window
  counter per API key, in-process.  Returns ``True`` if the request
  is allowed, ``False`` if the cap is reached.  Per-process scope
  means a multi-worker deployment effectively allows ``N_workers ×
  limit`` requests per minute; for strict cluster-wide limiting put
  ``nginx limit_req`` (or a Redis-backed limiter) in front.
  Documented in the config table.

- ``todays_live_run_cost(conn, project_id)`` — cheap PK lookup
  against ``runs`` to read the cumulative cost the proxy has
  charged today.  Compared against the project config's
  ``cost_cap_usd_daily`` to refuse calls past budget.

In-memory state is deliberate.  Adding Redis as a dep would
sharpen the limit but expand the surface area; the current design
matches the rest of the OSS stack ("SQLite-by-default, Postgres
when you need it, no other moving parts").  A future
``[quota-backend]`` extra could plug in Redis without changing
the call site.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Connection


# Default rate limit — generous baseline so a typical eval-driven
# workload doesn't trip it accidentally.  Operators tune per-project
# via ``rate_limit_per_minute`` in the YAML config.
DEFAULT_RATE_LIMIT_PER_MINUTE: Final[int] = 60

# Sliding-window width.  60s is the natural unit for ``per minute``;
# fine-grained windows (e.g. 1s) would catch burst patterns better
# but require more bookkeeping.
_WINDOW_S: Final[float] = 60.0

# ``deque`` per key — maxlen caps memory if a key bursts past the
# limit (we still drop old timestamps in the prune step below).
# 600 entries × ~50 bytes/entry × N_keys = bounded memory footprint
# even for thousands of distinct keys.
_LIMITER_STATE: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=600))

# Single lock for the whole dict.  Per-key locks would scale better
# under high concurrency but the GIL serialises the dict-mutation
# critical sections anyway; this lock just makes the prune+append
# atomic relative to other workers in the same process.
_LIMITER_LOCK: Final[Lock] = Lock()


def rate_limit_check(key_id: str, *, limit_per_minute: int) -> bool:
    """Return True if the request is allowed, False if the per-key
    cap is exceeded.  Updates state as a side effect — call exactly
    once per request, BEFORE the expensive work.

    A ``limit_per_minute`` ≤ 0 disables the check (always allows).
    Useful for an operator who wants to keep the limiter
    instrumented but disabled for a specific project.
    """
    if limit_per_minute <= 0:
        return True
    now = time.monotonic()
    cutoff = now - _WINDOW_S
    with _LIMITER_LOCK:
        history = _LIMITER_STATE[key_id]
        # Drop timestamps outside the sliding window so a long-idle
        # key doesn't trip the cap on a single request.
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= limit_per_minute:
            return False
        history.append(now)
        return True


def reset_rate_limiter() -> None:
    """Drop all in-memory state.  Tests call this between cases so a
    key's history doesn't bleed across them; production never needs
    it (process restart achieves the same effect)."""
    with _LIMITER_LOCK:
        _LIMITER_STATE.clear()


def todays_live_run_cost(conn: Connection, project_id: str) -> float:
    """Return the cumulative ``cost_usd`` the proxy has charged
    against this project today.  ``0.0`` when no row exists yet
    (first call of the day hasn't lazy-created it).

    Cheap: PK lookup on ``runs.run_id`` via the deterministic
    ``live_run_id`` derivation.  No scan, no aggregation.
    """
    # Import here to avoid a top-level circular import between
    # ``quotas`` (this module) and ``live`` (which doesn't import
    # quotas but the API's startup graph can be sensitive).
    from evalguard_api.live import live_run_id, utc_date_str

    run_id = live_run_id(project_id, utc_date_str())
    row = conn.execute(
        text("SELECT cost_usd FROM runs WHERE run_id = :rid"),
        {"rid": run_id},
    ).first()
    return float(row[0]) if row and row[0] is not None else 0.0
