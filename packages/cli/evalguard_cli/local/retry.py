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
    # 408 Request Timeout, 425 Too Early, 449 Retry With (some proxies)
    r"\b40[8]\b",
    r"\b42[5]\b",
    r"\b449\b",
    r"rate.?limit",
    r"timeout",
    r"timed.?out",
    r"connection.?(reset|refused|aborted|error)",
    r"temporarily.?unavailable",
    # ``server.?overloaded`` already covered the explicit proxy phrasing;
    # ``\boverloaded\b`` adds Anthropic's ``overloaded_error`` and
    # similar bare wording from other vendor SDKs.
    r"\boverloaded\b",
    r"server.?overloaded",
    r"service.?unavailable",
)


class ProviderFailed(Exception):
    """Raised when the retry budget is exhausted (or the error was
    non-retryable). Carries the original cause, the per-attempt
    summary, and the total billed cost across all attempts so the
    executor can credit it to the run total instead of silently
    under-counting (real providers like OpenAI bill some 429-after-
    partial-generation cases — a provider that wants to surface that
    can set ``e.cost_usd`` on the raised exception).
    """

    def __init__(
        self,
        *,
        attempts: list[dict[str, Any]],
        cause: BaseException,
        total_cost_usd: float = 0.0,
        cancelled: bool = False,
    ) -> None:
        super().__init__(f"provider failed after {len(attempts)} attempt(s): {cause}")
        self.attempts = attempts
        self.cause = cause
        # Sum of ``attempts[*]['cost_usd']`` — surfaced so the executor
        # can add it to ``total_cost`` even though the call ultimately
        # failed.
        self.total_cost_usd = total_cost_usd
        # ``True`` when the retry loop aborted because the cancel
        # event fired (cost cap reached, fail_fast triggered, etc.).
        # The executor treats cancelled differently from genuine
        # failure: don't mark the row as ``provider.failed``, just
        # skip it cleanly.
        self.cancelled = cancelled


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
    cancel: asyncio.Event | None = None,
) -> ProviderResult:
    """Invoke ``coro_factory()`` with retries.

    ``coro_factory`` is a zero-arg callable returning a fresh awaitable
    per attempt — each retry MUST start a new coroutine since
    coroutines are single-use.

    ``on_retry(attempt, exc, delay_ms)`` is called once before each
    retry (not before the first attempt, not after the last failure).
    The executor uses this hook to emit ``provider.retry`` events.

    ``cancel`` is an optional ``asyncio.Event``. When set (e.g. by the
    cost-cap pre-flight on another concurrent row, or by ``fail_fast``)
    the retry loop aborts BEFORE the next sleep / attempt, raising
    ``ProviderFailed(cancelled=True, ...)`` so the executor can skip
    the row cleanly without burning more retry budget. Without this
    hook, a 429 storm at ``max_retries: 5`` could keep spending after
    the cap fires elsewhere.

    Final failure raises ``ProviderFailed`` with per-attempt summary
    and the total billed cost (sum of ``attempts[*]['cost_usd']``,
    populated when a provider raises an exception with a ``cost_usd``
    attribute attached).
    """
    attempts: list[dict[str, Any]] = []
    last_exc: BaseException | None = None

    for attempt in range(policy.max_retries + 1):
        # Re-check cancel BEFORE each attempt — a concurrent row may
        # have fired the cap during the previous backoff sleep.
        if cancel is not None and cancel.is_set():
            return _raise_cancelled(attempts, last_exc)
        t0 = time.monotonic()
        try:
            return await coro_factory()
        except Exception as e:  # noqa: BLE001 — provider error surface is wide
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            # Providers that bill on failure (rare but possible) can
            # attach ``cost_usd`` to the exception. Default 0.
            attempt_cost = float(getattr(e, "cost_usd", 0.0) or 0.0)
            attempts.append({
                "attempt":     attempt,
                "duration_ms": elapsed_ms,
                "error":       str(e)[:240],
                "error_type":  type(e).__name__,
                "retryable":   policy.is_retryable(e),
                "cost_usd":    attempt_cost,
            })
            last_exc = e
            retryable = policy.is_retryable(e)
            is_last = attempt >= policy.max_retries
            if (not retryable) or is_last:
                raise ProviderFailed(
                    attempts=attempts,
                    cause=e,
                    total_cost_usd=sum(a["cost_usd"] for a in attempts),
                ) from e
            delay_ms = policy.delay_ms(attempt)
            if on_retry is not None:
                on_retry(attempt + 1, e, delay_ms)
            # Sleep in small slices so the cancel event short-circuits
            # the backoff promptly (a 30 s ``max_delay_ms`` would
            # otherwise let the run keep running for half a minute
            # past cost-cap).
            if delay_ms > 0:
                if cancel is not None:
                    waited = 0
                    slice_ms = 100
                    while waited < delay_ms:
                        if cancel.is_set():
                            return _raise_cancelled(attempts, last_exc)
                        step = min(slice_ms, delay_ms - waited)
                        await sleep(step / 1000.0)
                        waited += step
                else:
                    await sleep(delay_ms / 1000.0)

    # Unreachable: the loop either returns or raises ProviderFailed.
    raise ProviderFailed(
        attempts=attempts,
        cause=last_exc or RuntimeError("no attempts"),
        total_cost_usd=sum(a.get("cost_usd", 0.0) for a in attempts),
    )


def _raise_cancelled(
    attempts: list[dict[str, Any]],
    last_exc: BaseException | None,
) -> ProviderResult:
    """Raise a ``ProviderFailed(cancelled=True, ...)``. Pulled into
    a helper so the inner loop reads cleanly. Returns ``ProviderResult``
    in the type signature but always raises — the return type matches
    the caller's expectation."""
    raise ProviderFailed(
        attempts=attempts,
        cause=last_exc or RuntimeError("call cancelled before first attempt"),
        total_cost_usd=sum(a.get("cost_usd", 0.0) for a in attempts),
        cancelled=True,
    )
