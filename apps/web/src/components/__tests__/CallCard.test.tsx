import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { CallCard } from "../CallCard";
import type { CallSummary } from "@/lib/api";


function makeCall(overrides: Partial<CallSummary> = {}): CallSummary {
  return {
    run_id: "run_abcdef01234567",
    row_id: "r-7",
    trial_id: "trial_a",
    project_id: "proj_x",
    passed: true,
    cost_usd: 0.0123,
    latency_ms: 142,
    cache_hit: false,
    tags: [],
    ingested_at: "2026-05-15T07:30:00",
    output_preview: "the configured LLM said the request was handled.",
    ...overrides,
  };
}


describe("<CallCard>", () => {
  it("renders PASS / FAIL badge from ``passed``", () => {
    const { rerender } = render(
      <CallCard call={makeCall({ passed: true })} onSelect={() => {}} />,
    );
    expect(screen.getByText("PASS")).toBeInTheDocument();
    rerender(<CallCard call={makeCall({ passed: false })} onSelect={() => {}} />);
    expect(screen.getByText("FAIL")).toBeInTheDocument();
  });


  it("emits row_id + run_id via data-* attrs for stable e2e addressing", () => {
    const { container } = render(
      <CallCard call={makeCall({ run_id: "run_target", row_id: "r-target" })} onSelect={() => {}} />,
    );
    const card = container.querySelector('[data-testid="call-card"]');
    expect(card?.getAttribute("data-run-id")).toBe("run_target");
    expect(card?.getAttribute("data-row-id")).toBe("r-target");
    // ``data-passed`` so a filter test can select pass-rows without
    // reading the rendered Badge text.
    expect(card?.getAttribute("data-passed")).toBe("true");
  });


  it("renders latency + cost with the right formatting", () => {
    render(
      <CallCard
        call={makeCall({ latency_ms: 142, cost_usd: 0.012345 })}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("142 ms")).toBeInTheDocument();
    // Cost is always 4-decimal so columns line up.
    expect(screen.getByText("$0.0123")).toBeInTheDocument();
  });


  it("shows up to three tags as info badges", () => {
    render(
      <CallCard
        call={makeCall({ tags: ["edge", "vip", "regression", "extra-not-shown"] })}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("edge")).toBeInTheDocument();
    expect(screen.getByText("vip")).toBeInTheDocument();
    expect(screen.getByText("regression")).toBeInTheDocument();
    // Cap at 3 — the fourth tag must NOT render.
    expect(screen.queryByText("extra-not-shown")).not.toBeInTheDocument();
  });


  it("renders a cache badge only when cache_hit is true", () => {
    const { rerender } = render(
      <CallCard call={makeCall({ cache_hit: false })} onSelect={() => {}} />,
    );
    expect(screen.queryByText("cache")).not.toBeInTheDocument();
    rerender(<CallCard call={makeCall({ cache_hit: true })} onSelect={() => {}} />);
    expect(screen.getByText("cache")).toBeInTheDocument();
  });


  it("renders the output preview and uses it as the title for hover", () => {
    const preview = "this is a real LLM response that should be visible";
    const { container } = render(
      <CallCard call={makeCall({ output_preview: preview })} onSelect={() => {}} />,
    );
    const p = container.querySelector("p");
    expect(p).not.toBeNull();
    expect(p?.textContent).toBe(preview);
    expect(p?.getAttribute("title")).toBe(preview);
  });


  it("omits the preview block entirely when output_preview is null", () => {
    const { container } = render(
      <CallCard call={makeCall({ output_preview: null })} onSelect={() => {}} />,
    );
    expect(container.querySelector("p")).toBeNull();
  });


  it("fires onSelect with the call when clicked", () => {
    const onSelect = vi.fn();
    const call = makeCall({ run_id: "run_clicktarget", row_id: "r-clicked" });
    render(<CallCard call={call} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("call-card"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(call);
  });


  it("reflects selected state via aria-pressed for accessibility tooling", () => {
    const { rerender, container } = render(
      <CallCard call={makeCall()} selected={false} onSelect={() => {}} />,
    );
    expect(
      container.querySelector('[data-testid="call-card"]')?.getAttribute("aria-pressed"),
    ).toBe("false");
    rerender(<CallCard call={makeCall()} selected onSelect={() => {}} />);
    expect(
      container.querySelector('[data-testid="call-card"]')?.getAttribute("aria-pressed"),
    ).toBe("true");
  });
});
