import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { AssetVersionsTable } from "../AssetVersionsTable";
import type { AssetVersionRecord } from "@/lib/api";


function makeRecord(overrides: Partial<AssetVersionRecord> = {}): AssetVersionRecord {
  return {
    version_id: "sha256-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    run_id:     "run_abcdef01234567",
    project_name: "demo",
    ingested_at: "2026-05-14T07:30:00",
    source: "cli",
    ...overrides,
  };
}


describe("<AssetVersionsTable>", () => {
  it("renders 'No ingests yet' on empty input", () => {
    render(<AssetVersionsTable versions={[]} />);
    expect(screen.getByText(/no ingests yet/i)).toBeInTheDocument();
  });

  it("renders one row per record with truncated ids", () => {
    const rows = [
      makeRecord({ run_id: "run_aaaaaaaaaaaaaaaaaaaa" }),
      makeRecord({
        run_id:     "run_bbbbbbbbbbbbbbbbbbbb",
        version_id: "sha256-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      }),
    ];
    render(<AssetVersionsTable versions={rows} />);
    const tableRows = screen.getAllByTestId("asset-version-row");
    expect(tableRows).toHaveLength(2);
    // Truncation uses U+2026 (single-char ellipsis), not three dots.
    const firstVersion = tableRows[0].querySelector("td")!;
    expect(firstVersion.textContent).toMatch(/…$/);
    expect(firstVersion.textContent!.length).toBeLessThanOrEqual(17);
    // ``title`` attribute preserves the full id so a hover reveals
    // the un-truncated value.
    expect(firstVersion.getAttribute("title")).toBe(rows[0].version_id);
  });

  it("exposes data-version-id / data-run-id for parent-level addressing", () => {
    const rows = [makeRecord({ run_id: "run_target_xxx", version_id: "vid_target" })];
    const { container } = render(<AssetVersionsTable versions={rows} />);
    const card = container.querySelector('[data-testid="asset-version-row"]');
    expect(card?.getAttribute("data-version-id")).toBe("vid_target");
    expect(card?.getAttribute("data-run-id")).toBe("run_target_xxx");
  });

  it("badges otlp-sourced records distinctly from cli", () => {
    render(<AssetVersionsTable versions={[
      makeRecord({ source: "cli" }),
      makeRecord({ source: "otlp" }),
    ]} />);
    // Two badges side-by-side ⇒ both labels render once.
    expect(screen.getByText("cli")).toBeInTheDocument();
    expect(screen.getByText("otlp")).toBeInTheDocument();
  });

  it("links each run to its detail page", () => {
    render(<AssetVersionsTable versions={[
      makeRecord({ run_id: "run_link_target_aa" }),
    ]} />);
    const link = screen.getByRole("link");
    // Next.js's static-export ``<Link>`` may collapse a trailing
    // slash before the query string (``/runs/detail/?id=`` →
    // ``/runs/detail?id=``).  Either is fine — assert on the query
    // portion which is the stability contract for run-detail
    // routing.
    const href = link.getAttribute("href") ?? "";
    expect(href).toMatch(/^\/runs\/detail\/?\?id=run_link_target_aa$/);
  });

  it("keys rows uniquely even when the same (version, run) repeats", () => {
    // Same version_id + run_id appearing twice should not produce
    // a duplicate React key warning — the in-component key includes
    // the index.  We assert the rows render rather than the absence
    // of a console warning (which @testing-library doesn't trap).
    const dup = makeRecord();
    render(<AssetVersionsTable versions={[dup, dup]} />);
    expect(screen.getAllByTestId("asset-version-row")).toHaveLength(2);
  });
});
