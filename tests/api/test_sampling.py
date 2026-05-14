"""Phase 3c — head-based sampler unit tests.

The sampler is a pure function (``keep``) and a pure body-walker
(``filter_otlp_spans``). Route-level integration is in
``test_otlp.py``; here we exercise the math.

Determinism is the load-bearing property: the same ``trace_id`` must
produce the same verdict across processes, restarts, and Python's
``hash()`` randomization.  Two of these tests pin that specifically.
"""

from __future__ import annotations

import hashlib

import pytest

from evalguard_api.sampling import filter_otlp_spans, keep


# ---------------------------------------------------------------------------
# ``keep`` — boundary conditions


def test_rate_one_keeps_everything():
    assert keep("any-trace", 1.0) is True
    assert keep("",          1.0) is True
    # Any traceId, including an arbitrarily ugly one.
    assert keep("\x00\xff!@#", 1.0) is True


def test_rate_zero_drops_everything_except_empty():
    # Empty trace_id is always kept — see the docstring's reasoning
    # (parser handles "no trace id" as a parsing oddity, not a
    # sampling decision).
    assert keep("",                                      0.0) is True
    assert keep("abcdef0123456789abcdef0123456789",      0.0) is False
    assert keep("trace-deadbeef",                        0.0) is False


def test_rate_clamps_to_endpoints():
    # Out-of-range rates should still be safe — clamp to the nearest
    # endpoint rather than raising.
    assert keep("t1",  2.0) is True   # ≥ 1.0 fast path
    assert keep("t1", -0.5) is False  # ≤ 0.0 fast path


# ---------------------------------------------------------------------------
# Determinism


def test_same_trace_id_always_gets_the_same_verdict():
    """Cardinal property — load-shedding *would* split a trace if
    this were random."""
    trace_id = "deadbeefcafe1234deadbeefcafe1234"
    rate = 0.5
    first = keep(trace_id, rate)
    for _ in range(50):
        assert keep(trace_id, rate) is first


def test_verdict_uses_sha256_not_python_hash():
    """Python's ``hash()`` is randomized across processes by default
    (``PYTHONHASHSEED=random``), so a sampler that used it would
    sample differently across two API workers. SHA-256 is stable.

    We pin the verdict for a known traceId at a known rate so a
    refactor that reaches for ``hash()`` is caught immediately.
    """
    trace_id = "0011223344556677889900112233445566"
    digest_int = int.from_bytes(
        hashlib.sha256(trace_id.encode()).digest()[:8], "big",
    )
    # Pick a rate that puts our specific hash on a known side.
    threshold_below = digest_int / (1 << 64) - 1e-9
    threshold_above = digest_int / (1 << 64) + 1e-9
    assert keep(trace_id, threshold_below) is False
    assert keep(trace_id, threshold_above) is True


# ---------------------------------------------------------------------------
# ``keep`` — empirical rate over a large sample


@pytest.mark.parametrize("rate", [0.1, 0.25, 0.5, 0.9])
def test_acceptance_rate_matches_target_within_tolerance(rate: float):
    """Generate 5 000 distinct traceIds, compute the empirical kept-
    fraction, and assert it's within ±0.03 of ``rate``.

    The bound is loose enough to never flake (5σ for n=5 000 at
    rate=0.5 is ~0.014) while still catching any "off-by-one"
    threshold bugs that would shift the rate by 5 %+."""
    n = 5_000
    kept = sum(keep(f"trace-{i:08x}", rate) for i in range(n))
    observed = kept / n
    assert abs(observed - rate) < 0.03, (rate, observed)


# ---------------------------------------------------------------------------
# ``filter_otlp_spans`` — body-walker behaviour


def _otlp(spans: list[tuple[str, str]]) -> dict:
    """Build a minimal OTLP body with one ResourceSpans / ScopeSpans
    block carrying ``spans = [(trace_id, span_id), ...]``."""
    return {
        "resourceSpans": [{
            "resource": {"attributes": []},
            "scopeSpans": [{
                "scope": {"name": "test", "version": "0"},
                "spans": [
                    {"traceId": tid, "spanId": sid, "attributes": []}
                    for tid, sid in spans
                ],
            }],
        }],
    }


def test_filter_at_rate_one_is_a_passthrough_with_zero_drops():
    body = _otlp([("trace_a", "span1"), ("trace_b", "span2")])
    out, kept, dropped = filter_otlp_spans(body, 1.0)
    # Returned object IS the input — no copy on the hot path.
    assert out is body
    assert kept    == 2
    assert dropped == 0


def test_filter_at_rate_zero_drops_all_named_traces():
    body = _otlp([("trace_a", "span1"), ("trace_b", "span2")])
    out, kept, dropped = filter_otlp_spans(body, 0.0)
    assert kept    == 0
    assert dropped == 2
    # The structure is preserved, but ``spans`` is empty.
    assert out["resourceSpans"][0]["scopeSpans"][0]["spans"] == []


def test_filter_groups_spans_by_trace_id():
    """All spans of the same traceId share a verdict — never a half-
    rendered trace where some spans were kept and others dropped."""
    body = _otlp([
        ("trace_x", "s1"), ("trace_x", "s2"), ("trace_x", "s3"),
        ("trace_y", "s4"), ("trace_y", "s5"),
        ("trace_z", "s6"),
    ])
    # Use rate=0.5; whichever of x/y/z is kept, all of its spans
    # must be kept together.
    out, _, _ = filter_otlp_spans(body, 0.5)
    kept_spans = out["resourceSpans"][0]["scopeSpans"][0]["spans"]
    kept_traces = {s["traceId"] for s in kept_spans}
    counts = {tid: 0 for tid in ("trace_x", "trace_y", "trace_z")}
    for s in kept_spans:
        counts[s["traceId"]] += 1
    for tid, expected in (("trace_x", 3), ("trace_y", 2), ("trace_z", 1)):
        if tid in kept_traces:
            assert counts[tid] == expected, f"{tid} half-kept"
        else:
            assert counts[tid] == 0


def test_filter_does_not_mutate_input_body():
    body = _otlp([("trace_a", "span1"), ("trace_b", "span2")])
    body_before = body
    spans_before = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    n_before = len(spans_before)
    filter_otlp_spans(body, 0.0)
    # Input still has both spans.
    assert body is body_before
    assert len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]) == n_before


def test_filter_handles_malformed_body_without_crashing():
    # Non-dict body: parser will reject; sampler must hand it through.
    out, kept, dropped = filter_otlp_spans({"resourceSpans": "not a list"}, 0.5)
    assert kept == 0 and dropped == 0
    # Non-dict resourceSpans entry: skipped.
    out, kept, dropped = filter_otlp_spans(
        {"resourceSpans": ["garbage", {"scopeSpans": []}]}, 0.5,
    )
    assert kept == 0 and dropped == 0
