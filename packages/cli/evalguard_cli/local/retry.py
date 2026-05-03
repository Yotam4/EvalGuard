"""Provider-call retry policy.

Wraps a single ``await provider.complete(...)`` invocation with bounded
exponential backoff + jitter, audit-emitting each retry attempt and
the eventual failure if the call exhausts the budget.

Why this lives in ``cli/local`` and not in each provider:

- Centralizing retry keeps providers minimal — a plugin author writes
  a ``complete()`` and gets retry / audit / cost-cap interaction for
  free.
- Audit events (``provider.retry``, ``provider.failed``) carry trial /
  row context that providers don't have.
- Per-provider overrides remain possible by setting ``retry:`` under
  the provider's ``config:`` block.

What counts as retryable is matched against ``str(exception)`` because
the OSS tier can't depend on every vendor SDK to expose typed errors.
The default patterns cover the common transient failure modes.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from evalguard_evaluators import ProviderResult


# Default substring/regex patterns we treat as retryable. Match is on
# str(exc) (lowercased) and runs after the type name is prepended so
# ``RateLimitError("...")`` matches ``rate.?limit`` even when the
# message is empty.
_DEFAULT_RETRY_PATTERNS: tuple[str, ...] = (
    r"\b429\b",
    r"\b50[0-3]\b",
    r"rate.?limit",
    r"timeout",
    r"timed.?out",
    r"connection.?(reset|refused|aborted|error)",
    r"temporarily.?unavailable",
    r"server.?overloaded",
    r"service.?unavailable",
)


class ProviderFailed(Exception):
    """Raised when the retry budget is exhausted (or the error was
    non-retryable). Carries the original cause and the per-attempt
    summary so the executor can mark the row failed and continue."""

    def __init__(self, *, attempts: list[dict[str, Any]], cause: BaseException) -> None:
        super().__init__(f"provider failed after {len(attempts)} attempt(s): {cause}")
        self.attempts = attempts
        self.cause = cause


@dataclass
class RetryPolicy:
    """Bounded exponential-backoff retry policy.

    Total attempts = 1 (initial) + ``max_retries``. Set ``max_retries: 0``
    to disable retry entirely while still going through the audit /
    failure-handling path.
    """

    max_retries:    int   = 3
    base_delay_ms:  int   = 1_000
    max_delay_ms:   int   = 30_000
    jitter:         float = 0.25      # ±25% additive jitter on each delay
    retry_on:       tuple[str, ...] = field(default_factory=lambda: _DEFAULT_RETRY_PATTERNS)

    def __post_init__(self) -> None:
        # Pre-compile patterns once. Match against ``f"{type(e).__name__}: {e}"``
        # case-insensitively so users don't need anchors.
        self._compiled = tuple(re.compile(p, re.IGNORECASE) for p in self.retry_on)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "RetryPolicy":
        """Build from a YAML mapping, falling back to defaults for missing keys.

        Accepts ``None`` so callers don't have to guard.
        """
        if not cfg:
            return cls()
        return cls(
            max_retries   = int(cfg.get("max_retries",   cls.max_retries)),
            base_delay_ms = int(cfg.get("base_delay_ms", cls.base_delay_ms)),
            max_delay_ms  = int(cfg.get("max_delay_ms",  cls.max_delay_ms)),
            jitter        = float(cfg.get("jitter",      cls.jitter)),
            retry_on      = tuple(cfg.get("retry_on",    _DEFAULT_RETRY_PATTERNS)),
        )

    def is_retryable(self, exc: BaseException) -> bool:
        """True iff ``exc``'s type-name + message matches any retry pattern."""
        haystack = f"{type(exc).__name__}: {exc}"
        return any(p.search(haystack) for p in self._compiled)

    def delay_ms(self, attempt: int) -> int:
        """Backoff for the (post-failure) ``attempt``-th retry, 0-indexed.

        attempt=0 is the delay BEFORE the first retry (i.e. after the
        initial attempt failed). Capped at ``max_delay_ms`` and
        perturbed by ±jitter * delay.
        """
        base = min(self.base_delay_ms * (2 ** attempt), self.max_delay_ms)
        if self.jitter > 0:
            delta = base * self.jitter
            base = max(0, int(base + random.uniform(-delta, delta)))
        return base


async def call_with_retry(
    *,
    coro_factory: Callable[[], Awaitable[ProviderResult]],
    policy: RetryPolicy,
    on_retry: Callable[[int, BaseException, int], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ProviderResult:
    """Invoke ``coro_factory()`` with retries.

    ``coro_factory`` is a zero-arg callable returning a fresh awaitable
    per attempt — each retry MUST start a new coroutine since
    coroutines are single-use.

    ``on_retry(attempt, exc, delay_ms)`` is called once before each
    retry (not before the first attempt, not after the last failure).
    The executor uses this hook to emit ``provider.retry`` events.

    Final failure raises ``ProviderFailed`` with per-attempt summary.
    """
    attempts: list[dict[str, Any]] = []
    last_exc: BaseException | None = None

    for attempt in range(policy.max_retries + 1):
        t0 = time.monotonic()
        try:
            return await coro_factory()
        except Exception as e:  # noqa: BLE001 — provider error surface is wide
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            attempts.append({
                "attempt":     attempt,
                "duration_ms": elapsed_ms,
                "error":       str(e)[:240],
                "error_type":  type(e).__name__,
                "retryable":   policy.is_retryable(e),
            })
            last_exc = e
            retryable = policy.is_retryable(e)
            is_last = attempt >= policy.max_retries
            if (not retryable) or is_last:
                raise ProviderFailed(attempts=attempts, cause=e) from e
            delay_ms = policy.delay_ms(attempt)
            if on_retry is not None:
                on_retry(attempt + 1, e, delay_ms)
            if delay_ms > 0:
                await sleep(delay_ms / 1000.0)

    # Unreachable: the loop either returns or raises ProviderFailed.
    raise ProviderFailed(attempts=attempts, cause=last_exc or RuntimeError("no attempts"))
