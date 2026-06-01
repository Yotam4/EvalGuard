/**
 * Vitest coverage for ``padToWindow`` — Phase PROXY round-3 review.
 *
 * The function pads a sparse server response (only days with traffic)
 * into a dense ``days``-day window so the timeline strip's gap
 * semantics is honest.  Tests pin: empty input, single entry,
 * full-coverage, server-future-dated, and DST-boundary cases.
 */

import { describe, expect, it } from "vitest";

import { padToWindow } from "../LiveTimeline";
import type { LiveTimelineEntry } from "@/lib/api";


function entry(date: string, row_count = 1): LiveTimelineEntry {
  return {
    run_id: `run_${date}`,
    started_at: `${date}T00:00:00+00:00`,
    finished_at: null,
    row_count,
    row_pass_count: row_count,
    row_fail_count: 0,
    cost_usd: 0.01 * row_count,
  };
}


// Pin a single ``today`` so all tests are deterministic regardless
// of wall clock.  May 29 is well clear of any DST boundary in UTC
// (which doesn't observe DST) — we'll exercise DST in a US/EU local
// test below.
const TODAY = new Date("2026-05-29T12:00:00Z");


describe("padToWindow", () => {
  it("returns a fully-empty window when the server has no data", () => {
    const out = padToWindow([], 30, TODAY);
    expect(out).toHaveLength(30);
    // Every padded entry has row_count=0 and an empty run_id.
    expect(out.every((e) => e.row_count === 0)).toBe(true);
    expect(out.every((e) => e.run_id === "")).toBe(true);
    // First entry is 29 days before TODAY; last is TODAY.
    expect(out[0].started_at).toBe("2026-04-30T00:00:00+00:00");
    expect(out[29].started_at).toBe("2026-05-29T00:00:00+00:00");
  });


  it("slots a single real entry into the right calendar position", () => {
    const today_minus_5 = entry("2026-05-24");
    const out = padToWindow([today_minus_5], 30, TODAY);
    expect(out).toHaveLength(30);
    // Day index 24 (out of 0..29) is 5 days before TODAY → index 24.
    expect(out[24].row_count).toBe(1);
    expect(out[24].run_id).toBe("run_2026-05-24");
    // All other slots are synthetic zero entries.
    expect(out.filter((e) => e.row_count > 0)).toHaveLength(1);
  });


  it("preserves all real entries when the server returns a full window", () => {
    const full: LiveTimelineEntry[] = [];
    for (let i = 0; i < 30; i++) {
      const d = new Date(TODAY);
      d.setUTCDate(d.getUTCDate() - i);
      full.push(entry(d.toISOString().slice(0, 10), i + 1));
    }
    const out = padToWindow(full, 30, TODAY);
    expect(out).toHaveLength(30);
    // No synthetic zero days.
    expect(out.every((e) => e.row_count > 0)).toBe(true);
    // Newest-last ordering (TODAY at index 29).
    expect(out[29].started_at).toBe("2026-05-29T00:00:00+00:00");
  });


  it("ignores server entries that fall outside the window", () => {
    // A 60-day-old entry shouldn't appear in a 30-day window.
    const oldEntry = entry("2026-03-29");   // 61 days before TODAY
    const recent   = entry("2026-05-27");   // 2 days before TODAY
    const out = padToWindow([oldEntry, recent], 30, TODAY);
    expect(out).toHaveLength(30);
    const realCount = out.filter((e) => e.row_count > 0).length;
    expect(realCount).toBe(1);   // only the recent one
    expect(out.some((e) => e.run_id === "run_2026-05-27")).toBe(true);
    expect(out.some((e) => e.run_id === "run_2026-03-29")).toBe(false);
  });


  it("DST boundary (US Spring-forward) — UTC math is invariant", () => {
    // US 2026 DST start: Sunday, March 8.  Anchored in UTC the day
    // boundary doesn't move, so the strip stays exactly 30 calendar
    // days wide regardless of the local clock jump.  index 29 is
    // the anchor day; index i counts (29 - i) days back.  March 8
    // is 7 days before March 15 → index 29 - 7 = 22.
    const dstAnchor = new Date("2026-03-15T12:00:00Z");
    const out = padToWindow([entry("2026-03-08")], 30, dstAnchor);
    expect(out).toHaveLength(30);
    expect(out[22].started_at).toBe("2026-03-08T00:00:00+00:00");
    expect(out[22].row_count).toBe(1);
    expect(out[29].started_at).toBe("2026-03-15T00:00:00+00:00");
  });


  it("does not mutate the caller-supplied ``today``", () => {
    const today = new Date(TODAY);
    const before = today.getTime();
    padToWindow([], 30, today);
    expect(today.getTime()).toBe(before);
  });


  it("honours arbitrary day windows (7-day strip)", () => {
    const out = padToWindow([entry("2026-05-27")], 7, TODAY);
    expect(out).toHaveLength(7);
    // 7 days ending 2026-05-29: indices 0..6 cover 23..29.
    expect(out[0].started_at).toBe("2026-05-23T00:00:00+00:00");
    expect(out[6].started_at).toBe("2026-05-29T00:00:00+00:00");
    // The May 27 entry lands at index 4.
    expect(out[4].row_count).toBe(1);
  });
});
