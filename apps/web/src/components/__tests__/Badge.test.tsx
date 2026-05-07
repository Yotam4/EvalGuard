import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, statusTone } from "../Badge";

/**
 * ``statusTone`` is the single source of truth for how every page
 * colors a status string. Pin every documented status so a future
 * refactor can't silently change the color of, e.g., ``cost_capped``.
 */

describe("statusTone", () => {
  it.each([
    ["passed",      "pass"],
    ["warned",      "warn"],
    ["failed",      "fail"],
    ["row_failed",  "fail"],
    ["gate_failed", "fail"],
    ["cost_capped", "warn"],
    [null,          "muted"],
    [undefined,     "muted"],
    ["something-unknown", "muted"],
  ])("maps %s -> %s", (status, tone) => {
    expect(statusTone(status as string | null | undefined)).toBe(tone);
  });
});


describe("Badge", () => {
  it("renders its children", () => {
    render(<Badge>hello</Badge>);
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("applies a tone class", () => {
    const { container } = render(<Badge tone="pass">ok</Badge>);
    const span = container.querySelector("span");
    // Tone names propagate as part of the class string — checking for
    // the var() reference is the most stable assertion since the
    // exact Tailwind class composition can churn.
    expect(span?.className).toContain("--color-pass");
  });
});
