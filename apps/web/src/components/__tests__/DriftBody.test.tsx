import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  DriftBody, driftLabel, driftTone,
  fmtDelta, fmtMean, fmtP,
} from "../DriftBody";
import type { DriftMetric, DriftReport } from "@/lib/api";


function makeMetric(overrides: Partial<DriftMetric> = {}): DriftMetric {
  // Sensible defaults: a non-significant latency_ms metric.
  return {
    name: "latency_ms",
    n_current: 12, n_baseline: 12,
    mean_current: 100, mean_baseline: 100,
    delta_mean: 0,
    t_stat: 0, dof: 22,
    p_two_sided: 1.0, p_less: 0.5, p_greater: 0.5,
    significant_at_alpha: false,
    ...overrides,
  };
}


function makeReport(overrides: Partial<DriftReport> = {}): DriftReport {
  return {
    current_run_id:  "run_current0",
    baseline_run_id: "run_baseline",
    alpha:    0.05,
    metrics:  [],
    skipped:  [],
    ...overrides,
  };
}


describe("formatting helpers", () => {
  it("fmtMean uses 4 decimals for fractions, 3 for [1, 100), 1 for ≥100", () => {
    expect(fmtMean(0.0012345)).toBe("0.0012");
    expect(fmtMean(1.5)).toBe("1.500");
    expect(fmtMean(150.234)).toBe("150.2");
    expect(fmtMean(NaN)).toBe("—");
    expect(fmtMean(Infinity)).toBe("—");
  });

  it("fmtDelta uses real plus / Unicode minus signs", () => {
    expect(fmtDelta(50)).toBe("+50.000");      // (50 ∈ [1,100))
    expect(fmtDelta(-50)).toBe("−50.000");     // U+2212, not ASCII -
    expect(fmtDelta(0)).toBe("+0.0000");       // 0 is "non-negative"
    expect(fmtDelta(NaN)).toBe("—");
  });

  it("fmtP collapses sub-1e-4 p-values to a placeholder", () => {
    expect(fmtP(0.5)).toBe("0.5000");
    expect(fmtP(1e-30)).toBe("<1e-4");
    expect(fmtP(NaN)).toBe("—");
  });
});


describe("driftTone / driftLabel — direction semantics", () => {
  it("treats latency_ms ↑ as a regression (red)", () => {
    const m = makeMetric({ name: "latency_ms", delta_mean: 50, significant_at_alpha: true });
    expect(driftTone(m)).toBe("fail");
    expect(driftLabel(m)).toContain("↑");
  });

  it("treats latency_ms ↓ as an improvement (warn — flag for visibility)", () => {
    // Even an improvement is worth surfacing; "warn" tone draws the
    // eye without screaming "regression".
    const m = makeMetric({ name: "latency_ms", delta_mean: -50, significant_at_alpha: true });
    expect(driftTone(m)).toBe("warn");
    expect(driftLabel(m)).toContain("↓");
  });

  it("treats passed ↓ as a regression (pass-rate dropping is bad)", () => {
    const m = makeMetric({ name: "passed", delta_mean: -0.2, significant_at_alpha: true });
    expect(driftTone(m)).toBe("fail");
  });

  it("treats passed ↑ as an improvement", () => {
    const m = makeMetric({ name: "passed", delta_mean: 0.2, significant_at_alpha: true });
    expect(driftTone(m)).toBe("warn");
  });

  it("treats cost_usd ↑ as a regression", () => {
    const m = makeMetric({ name: "cost_usd", delta_mean: 0.5, significant_at_alpha: true });
    expect(driftTone(m)).toBe("fail");
  });
});


describe("<DriftBody>", () => {
  it("renders a 'no significant drift' badge when nothing is flagged", () => {
    render(<DriftBody report={makeReport({
      metrics: [makeMetric({ name: "latency_ms", significant_at_alpha: false })],
    })} />);
    expect(screen.getByText(/no significant drift/i)).toBeInTheDocument();
  });

  it("counts and pluralizes the significant-changes badge", () => {
    render(<DriftBody report={makeReport({
      metrics: [
        makeMetric({ name: "latency_ms", significant_at_alpha: true, delta_mean: 50 }),
        makeMetric({ name: "cost_usd",   significant_at_alpha: true, delta_mean: 0.001 }),
        makeMetric({ name: "passed",     significant_at_alpha: false }),
      ],
    })} />);
    expect(screen.getByText(/2 significant changes/i)).toBeInTheDocument();
  });

  it("uses the singular when exactly one metric is significant", () => {
    render(<DriftBody report={makeReport({
      metrics: [
        makeMetric({ name: "latency_ms", significant_at_alpha: true, delta_mean: 50 }),
      ],
    })} />);
    expect(screen.getByText(/1 significant change\b/i)).toBeInTheDocument();
    // The plural form must NOT be there.
    expect(screen.queryByText(/significant changes\b/i)).not.toBeInTheDocument();
  });

  it("renders the per-metric table with formatted numbers", () => {
    render(<DriftBody report={makeReport({
      metrics: [makeMetric({
        name: "latency_ms",
        n_current: 50, n_baseline: 50,
        mean_current: 150.234, mean_baseline: 100.123,
        delta_mean: 50.111, p_two_sided: 0.001,
        significant_at_alpha: true,
      })],
    })} />);
    expect(screen.getByText(/50 \/ 50/)).toBeInTheDocument();
    expect(screen.getByText(/150.2 \/ 100.1/)).toBeInTheDocument();
    expect(screen.getByText(/\+50.111/)).toBeInTheDocument();
    expect(screen.getByText(/0.0010/)).toBeInTheDocument();
  });

  it("surfaces skipped metrics with their reasons", () => {
    render(<DriftBody report={makeReport({
      skipped: [
        { name: "latency_ms", reason: "need ≥2 samples per side; got current=1, baseline=12" },
      ],
    })} />);
    const banner = screen.getByTestId("drift-skipped");
    expect(banner.textContent).toContain("latency_ms");
    expect(banner.textContent).toContain("≥2 samples");
  });
});
