"""Pure-Python statistical helpers for the API server.

Inlined copy of ``packages/cli/evalguard_cli/local/stats.py`` so the
server stays decoupled from the CLI package — the CLI is MIT-licensed
and shipped on its own cadence; pulling it in as a server dep would
couple their release cycles unnecessarily.

Parity with the CLI is pinned by ``tests/api/test_drift.py``'s
``test_welch_parity_with_cli_implementation`` — same inputs in,
same outputs out. If either side ever diverges, the test fails
loudly and we fix the duplication or extract a shared utility.

Used by Phase 3b's ``GET /v1/runs/{run_id}/drift`` endpoint to
compare per-row metric distributions (latency_ms, cost_usd,
passed) between two runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class WelchResult:
    """Output of Welch's two-sample, unequal-variance t-test."""

    t_stat:    float
    dof:       float
    p_two_sided: float
    p_less:    float           # P(true_diff < 0): "current < baseline"
    p_greater: float           # P(true_diff > 0)
    n1:        int
    n2:        int
    mean1:     float
    mean2:     float
    var1:      float
    var2:      float


def welchs_t_test(sample1: list[float], sample2: list[float]) -> WelchResult:
    """Welch's t-test for unequal variances. Returns t-stat, dof, and
    one/two-sided p-values.

    ``sample1`` is the "current run" / treatment side, ``sample2`` the
    baseline / control. Both samples must have at least 2 observations.
    """
    n1, n2 = len(sample1), len(sample2)
    if n1 < 2 or n2 < 2:
        raise ValueError(f"Welch's t-test needs ≥2 samples per side; got n1={n1}, n2={n2}")

    mean1 = sum(sample1) / n1
    mean2 = sum(sample2) / n2
    var1 = sum((x - mean1) ** 2 for x in sample1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in sample2) / (n2 - 1)
    se_sq = var1 / n1 + var2 / n2

    if se_sq == 0:
        # Identical, zero-variance samples — no statistical signal.
        # Avoid div-by-zero; report "no detectable difference".
        t = 0.0
        dof = float(n1 + n2 - 2)
        p_two = 1.0
        p_less = 0.5 if mean1 == mean2 else (1.0 if mean1 < mean2 else 0.0)
        p_greater = 1.0 - p_less
        return WelchResult(t, dof, p_two, p_less, p_greater,
                           n1, n2, mean1, mean2, var1, var2)

    t = (mean1 - mean2) / math.sqrt(se_sq)
    # Welch–Satterthwaite degrees of freedom.
    num = se_sq ** 2
    den = (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
    dof = num / den if den else float(n1 + n2 - 2)

    p_two = _student_t_sf_two_sided(t, dof)
    if t >= 0:
        p_less = 1.0 - p_two / 2
        p_greater = p_two / 2
    else:
        p_less = p_two / 2
        p_greater = 1.0 - p_two / 2
    return WelchResult(t, dof, p_two, p_less, p_greater,
                       n1, n2, mean1, mean2, var1, var2)


# ---------------------------------------------------------------------------
# Cumulative distribution helpers
#
# P(|T| > |t|) for T ~ Student-t(dof) = I_x(dof/2, 1/2) where
# x = dof / (dof + t^2). I_x(a,b) is the regularized incomplete beta
# function via continued-fraction expansion.


def _student_t_sf_two_sided(t: float, dof: float) -> float:
    if dof <= 0:
        return 1.0
    x = dof / (dof + t * t)
    return _regularized_incomplete_beta(x, dof / 2, 0.5)


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta I_x(a, b), x in [0, 1]. Numerical
    Recipes §6.4 — continued-fraction with symmetry-flip when x is
    on the slow-convergence side."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(x, a, b) / a
    return 1.0 - bt * _betacf(1.0 - x, b, a) / b


def _betacf(x: float, a: float, b: float, *, max_iter: int = 200, eps: float = 3.0e-7) -> float:
    """Modified Lentz's algorithm for the continued fraction."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1.0e-30:
        d = 1.0e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1.0e-30:
            d = 1.0e-30
        c = 1.0 + aa / c
        if abs(c) < 1.0e-30:
            c = 1.0e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1.0e-30:
            d = 1.0e-30
        c = 1.0 + aa / c
        if abs(c) < 1.0e-30:
            c = 1.0e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    return h
