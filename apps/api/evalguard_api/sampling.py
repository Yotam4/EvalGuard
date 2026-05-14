"""Probabilistic head-based sampler for OTLP ingest.

Phase 3c. A high-volume GenAI trace stream (e.g. a fleet of agents
emitting one span per LLM call at thousands of req/s) shouldn't drown
the EvalGuard API just because the operator forgot to configure
collector-side sampling. This module gates ``POST /v1/otlp/v1/traces``
at the API edge: each incoming request is parsed, and spans whose
``traceId`` hash falls above the configured rate are dropped before
they reach ``parse_traces``.

Design choices:

- **Trace-level, not span-level.**  The decision is keyed on
  ``traceId`` so every span in the same trace gets the same verdict.
  Span-level sampling would shred a trace into a half-rendered
  picture; the run synthesizer expects whole traces.

- **Deterministic.**  Same ``traceId`` ⇒ same answer across server
  restarts and across multiple collector retries.  The collector can
  redeliver a partial trace and we'll either keep or drop it
  consistently, never split.

- **Hash-based threshold (not RNG).**  ``rate * 2^64`` compared to
  the leading 8 bytes of ``sha256(traceId)``.  Avoids state, gives a
  uniform expected acceptance rate, and is cheap (one SHA-256 per
  trace).

- **Drops are silent at the OTLP layer.**  The OTLP/HTTP spec doesn't
  have "we sampled you out" — the collector treats 2xx as success.
  Dropping spans on our side and returning ``partialSuccess: {}`` is
  the right behaviour: the collector's reservoir-based retries are
  unrelated to our load shedding.  (Operators see drop counts in the
  structured access log.)

- **No outbound calls.**  All decisions are local.  Pinned for the
  air-gap audit in commit history.
"""

from __future__ import annotations

import hashlib
from typing import Any

# 2^64 — full hash range of the leading 8 bytes of SHA-256.
_HASH_DOMAIN: int = 1 << 64


def keep(trace_id: str, rate: float) -> bool:
    """Return True iff ``trace_id`` should be kept at sampling
    probability ``rate``.

    ``rate`` clamps to [0.0, 1.0]; values outside that range are
    treated as the nearest endpoint.  Empty / falsy ``trace_id`` is
    always kept — the absence of a trace id is a parsing oddity, not
    a load-shedding opportunity, so we route to the parser which has
    its own "skip span" logic.

    The hash uses SHA-256 (not the cheaper SipHash via Python's
    ``hash()``) because Python randomises ``hash()`` across processes
    by default — across two API workers the same ``trace_id`` could
    be sampled differently, defeating the determinism guarantee.
    """
    if not trace_id:
        return True
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
    h = int.from_bytes(digest[:8], "big")
    # Strict ``<`` so rate=0.0 never accepts (rate <= 0 short-circuit
    # already covers it; keeping the predicate consistent here too).
    return h < int(rate * _HASH_DOMAIN)


def filter_otlp_spans(
    body: dict[str, Any],
    rate: float,
) -> tuple[dict[str, Any], int, int]:
    """Walk an OTLP/HTTP JSON body and drop spans whose ``traceId``
    hash falls above ``rate``.

    Returns ``(filtered_body, kept_spans, dropped_spans)``.  The body
    is shallow-copied with new ResourceSpans / ScopeSpans / spans
    lists; the original is not mutated, so the caller can still log
    raw input on failure.

    ResourceSpans / ScopeSpans entries that end up with zero spans
    are kept in the structure (not pruned) — ``parse_traces`` already
    handles "no GenAI spans → no run", and dropping the parent here
    would mean a 100 %-non-GenAI ResourceSpans (e.g. a DB span block)
    looks identical to a sampled-out one.  Keeping them lets the
    parser be the single decision point.

    For ``rate >= 1.0`` we return the body unchanged with no copying
    — the hot path on the default ``EVALGUARD_OTLP_SAMPLE_RATE=1.0``
    is therefore zero-cost beyond a numeric compare.
    """
    if rate >= 1.0:
        return body, _count_total_spans(body), 0

    rs_list = body.get("resourceSpans")
    if not isinstance(rs_list, list):
        # Bad shape — let the parser raise the OtlpParseError so we
        # don't double-emit error paths.
        return body, 0, 0

    new_rs: list[dict] = []
    kept = dropped = 0

    for rs in rs_list:
        if not isinstance(rs, dict):
            new_rs.append(rs)
            continue
        new_scopes: list[dict] = []
        for scope in (rs.get("scopeSpans") or []):
            if not isinstance(scope, dict):
                new_scopes.append(scope)
                continue
            kept_spans: list[dict] = []
            for span in (scope.get("spans") or []):
                if not isinstance(span, dict):
                    # Hand it through unchanged; parser's per-span
                    # ``isinstance`` check will skip it.
                    kept_spans.append(span)
                    continue
                trace_id = str(span.get("traceId") or "")
                if keep(trace_id, rate):
                    kept_spans.append(span)
                    kept += 1
                else:
                    dropped += 1
            new_scopes.append({**scope, "spans": kept_spans})
        new_rs.append({**rs, "scopeSpans": new_scopes})

    return {**body, "resourceSpans": new_rs}, kept, dropped


def _count_total_spans(body: Any) -> int:
    """Count spans in an OTLP body without filtering — used so the
    fast path (rate=1.0) still reports the same ``kept`` number to
    the caller's structured log."""
    if not isinstance(body, dict):
        return 0
    total = 0
    for rs in (body.get("resourceSpans") or []):
        if not isinstance(rs, dict):
            continue
        for scope in (rs.get("scopeSpans") or []):
            if not isinstance(scope, dict):
                continue
            spans = scope.get("spans") or []
            if isinstance(spans, list):
                total += sum(1 for s in spans if isinstance(s, dict))
    return total
