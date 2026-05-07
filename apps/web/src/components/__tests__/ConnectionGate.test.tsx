import { describe, expect, it, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { ConnectionGate } from "../ConnectionGate";
import { setServerUrl, setToken } from "../../lib/auth";

/**
 * The gate is a route-level guard. When localStorage is empty it
 * must NOT render its children — otherwise the underlying page
 * would issue an unauthenticated fetch and surface a confusing
 * error instead of the actionable "open Settings" prompt.
 */

describe("<ConnectionGate>", () => {
  beforeEach(() => window.localStorage.clear());

  it("renders the Settings prompt when nothing is configured", async () => {
    render(
      <ConnectionGate>
        <div data-testid="protected">child content</div>
      </ConnectionGate>,
    );
    // First useEffect tick — the gate reads localStorage and decides.
    await waitFor(() =>
      expect(screen.getByText(/Server not configured/i)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("protected")).not.toBeInTheDocument();
    // The "Open Settings" CTA links to /settings.
    const link = screen.getByRole("link", { name: /Open Settings/i });
    expect(link.getAttribute("href")).toBe("/settings");
  });

  it("renders children when both URL and token are set", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    render(
      <ConnectionGate>
        <div data-testid="protected">child content</div>
      </ConnectionGate>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("protected")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Server not configured/i)).not.toBeInTheDocument();
  });

  it("requires BOTH a URL and a token (URL alone is not enough)", async () => {
    setServerUrl("https://api.example.com");
    // No token set.
    render(
      <ConnectionGate>
        <div data-testid="protected">child content</div>
      </ConnectionGate>,
    );
    await waitFor(() =>
      expect(screen.getByText(/Server not configured/i)).toBeInTheDocument(),
    );
  });
});
