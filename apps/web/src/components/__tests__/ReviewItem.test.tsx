import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ReviewItem } from "../ReviewItem";
import type { ReviewQueueItem } from "@/lib/api";


function makeItem(overrides: Partial<ReviewQueueItem> = {}): ReviewQueueItem {
  return {
    run_id: "run_test000",
    row_id: "r-fail",
    trial_id: "trial_a",
    project_id: "proj_x",
    passed: false,
    cost_usd: 0.002,
    latency_ms: 150,
    tags: ["edge"],
    failing_gates: ["min_pass_rate"],
    ...overrides,
  };
}


describe("<ReviewItem>", () => {
  it("renders row_id, failing gates and tags", () => {
    render(<ReviewItem item={makeItem()} onSubmit={() => {}} />);
    expect(screen.getByText("r-fail")).toBeInTheDocument();
    expect(screen.getByText("min_pass_rate")).toBeInTheDocument();
    expect(screen.getByText("edge")).toBeInTheDocument();
    // Cost / latency surface formatted next to each other.
    expect(screen.getByText(/\$0\.0020/)).toBeInTheDocument();
    expect(screen.getByText(/150 ms/)).toBeInTheDocument();
  });

  it("disables Submit until a verdict is picked", () => {
    render(<ReviewItem item={makeItem()} onSubmit={() => {}} />);
    const submit = screen.getByRole("button", { name: /submit review/i });
    expect(submit).toBeDisabled();
  });

  it("enables Submit after a verdict is picked + calls onSubmit with verdict and note", () => {
    const onSubmit = vi.fn();
    render(<ReviewItem item={makeItem()} onSubmit={onSubmit} />);

    // Pick a verdict.
    const overrideBtn = screen.getByRole("button", { name: /override → pass/i });
    fireEvent.click(overrideBtn);
    expect(overrideBtn.getAttribute("aria-pressed")).toBe("true");

    // Fill the note (with surrounding whitespace — the component
    // should strip it before passing to onSubmit).
    const note = screen.getByPlaceholderText(/optional note/i) as HTMLTextAreaElement;
    fireEvent.change(note, { target: { value: "  false-positive  " } });

    const submit = screen.getByRole("button", { name: /submit review/i });
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("override_pass", "false-positive");
  });

  it("does not double-submit while submitting=true", () => {
    const onSubmit = vi.fn();
    render(<ReviewItem item={makeItem()} onSubmit={onSubmit} submitting />);
    // Pick a verdict so we'd otherwise be ready.
    fireEvent.click(screen.getByRole("button", { name: /agree/i }));
    const submit = screen.getByRole("button", { name: /submitting/i });
    // Button is disabled while the parent's mutation is in-flight.
    expect(submit).toBeDisabled();
  });

  it("does not render the tags row when there are no tags", () => {
    render(<ReviewItem item={makeItem({ tags: [] })} onSubmit={() => {}} />);
    expect(screen.queryByText(/^tags:/)).not.toBeInTheDocument();
  });

  it("does not render the failing-gates row when none failed", () => {
    render(<ReviewItem item={makeItem({ failing_gates: [] })} onSubmit={() => {}} />);
    expect(screen.queryByText(/failing gates:/)).not.toBeInTheDocument();
  });

  it("exposes a data-row-id attribute for parent-level addressing", () => {
    // The /reviews page maps queue items by row_id; pinning the
    // attribute means a parent test can ``queryByTestId`` and then
    // pluck the right row without depending on text content.
    const { container } = render(
      <ReviewItem item={makeItem({ row_id: "row-XYZ" })} onSubmit={() => {}} />,
    );
    const card = container.querySelector('[data-testid="review-item"]');
    expect(card?.getAttribute("data-row-id")).toBe("row-XYZ");
  });
});
