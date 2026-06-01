import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { CallDetailPanel } from "../CallDetailPanel";
import type { CallDetail, Score, Gate } from "@/lib/api";


function makeDetail(overrides: Partial<CallDetail> = {}): CallDetail {
  return {
    run_id: "run_a",
    row_id: "r-1",
    trial_id: "trial_a",
    project_id: "proj_x",
    project: "demo",
    ingested_at: "2026-05-15T07:30:00",
    provider: "mock",
    model: "m",
    passed: true,
    n_scores: 0,
    cost_usd: 0.0123,
    latency_ms: 142,
    cache_hit: false,
    tags: [],
    input: "what is X?",
    expected: null,
    output: "X is …",
    scores: [],
    trial_gates: [],
    error: null,
    ...overrides,
  };
}


function makeScore(overrides: Partial<Score> = {}): Score {
  return {
    evaluator_id: "lex.faithfulness",
    evaluator_kind: "heuristic",
    layer: 2,
    value: 0.78,
    passed: true,
    ...overrides,
  } as Score;
}


function makeGate(overrides: Partial<Gate> = {}): Gate {
  return {
    gate_name: "min_pass_rate",
    severity: "block",
    blocking: true,
    passed: true,
    layer: 2,
    details: [],
    ...overrides,
  } as Gate;
}


describe("<CallDetailPanel>", () => {
  it("renders the header with row_id, provider:model, and verdict badge", () => {
    render(<CallDetailPanel data={makeDetail({ passed: false })} />);
    expect(screen.getByRole("heading", { name: "r-1" })).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText(/mock:m/)).toBeInTheDocument();
  });


  it("renders the error banner when ``error`` is populated (PROXY-2.5 review-pass)", () => {
    render(<CallDetailPanel data={makeDetail({
      passed: false,
      error: "TimeoutError: provider 'openai:gpt-4o' did not respond within 60s",
    })} />);
    const banner = screen.getByTestId("call-detail-error");
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent(/timeouterror/i);
  });


  it("omits the error banner when ``error`` is null", () => {
    render(<CallDetailPanel data={makeDetail({ error: null })} />);
    expect(screen.queryByTestId("call-detail-error")).not.toBeInTheDocument();
  });


  it("renders input / expected / output blocks with the right labels", () => {
    render(<CallDetailPanel data={makeDetail({
      input: "what is X?", expected: "X is Y.", output: "X is Z.",
    })} />);
    // ``data-testid`` lets us address each block without relying on
    // visible label text — useful when the labels get localised.
    const inputBlock    = screen.getByTestId("call-content-input");
    const expectedBlock = screen.getByTestId("call-content-expected");
    const outputBlock   = screen.getByTestId("call-content-output");
    expect(inputBlock.textContent).toContain("what is X?");
    expect(expectedBlock.textContent).toContain("X is Y.");
    expect(outputBlock.textContent).toContain("X is Z.");
  });


  it("shows em-dash placeholder when a content field is null", () => {
    render(<CallDetailPanel data={makeDetail({
      input: null, expected: null, output: null,
    })} />);
    // Three em-dashes — one per content block.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });


  it("pretty-prints structured input as JSON", () => {
    render(<CallDetailPanel data={makeDetail({
      input: { role: "user", content: "hi" },
    })} />);
    const inputBlock = screen.getByTestId("call-content-input");
    // Pretty-printed (newline-separated) so the structure stays
    // visible at a glance.
    expect(inputBlock.textContent).toContain("\"role\"");
    expect(inputBlock.textContent).toContain("\"content\"");
  });


  it("hides the scores section when there are zero scores", () => {
    const { container } = render(
      <CallDetailPanel data={makeDetail({ scores: [] })} />,
    );
    expect(container.querySelector('[data-testid="call-score-row"]')).toBeNull();
  });


  it("renders one row per score with evaluator_id + value + verdict", () => {
    render(<CallDetailPanel data={makeDetail({
      scores: [
        makeScore({ evaluator_id: "lex.faithfulness", value: 0.78, passed: true }),
        makeScore({ evaluator_id: "judge.q",          value: 4.50, passed: false }),
      ],
    })} />);
    const rows = screen.getAllByTestId("call-score-row");
    expect(rows).toHaveLength(2);
    expect(rows[0].getAttribute("data-evaluator-id")).toBe("lex.faithfulness");
    expect(rows[1].getAttribute("data-evaluator-id")).toBe("judge.q");
    // The two-decimal "1<=x<100" formatting from fmtScore.
    expect(rows[1].textContent).toContain("4.500");
  });


  it("hides the trial-gates section when empty, shows it when populated", () => {
    const { rerender } = render(
      <CallDetailPanel data={makeDetail({ trial_gates: [] })} />,
    );
    expect(screen.queryByText(/trial gates/i)).not.toBeInTheDocument();
    rerender(<CallDetailPanel data={makeDetail({
      trial_gates: [makeGate({ gate_name: "min_pass_rate", passed: false })],
    })} />);
    expect(screen.getByText(/trial gates/i)).toBeInTheDocument();
    expect(screen.getByText("min_pass_rate")).toBeInTheDocument();
  });


  it("fires onClose when the close button is clicked", () => {
    const onClose = vi.fn();
    render(<CallDetailPanel data={makeDetail()} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("call-detail-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });


  it("hides the close button when onClose is not supplied", () => {
    render(<CallDetailPanel data={makeDetail()} />);
    expect(screen.queryByTestId("call-detail-close")).toBeNull();
  });


  it("exposes data-run-id + data-row-id on the panel root for e2e", () => {
    const { container } = render(
      <CallDetailPanel data={makeDetail({ run_id: "run_t", row_id: "r-t" })} />,
    );
    const root = container.querySelector('[data-testid="call-detail-panel"]');
    expect(root?.getAttribute("data-run-id")).toBe("run_t");
    expect(root?.getAttribute("data-row-id")).toBe("r-t");
  });
});
