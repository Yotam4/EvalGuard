import { describe, expect, it } from "vitest";

import { filterAndSort } from "../page";
import type { GoldenCandidate } from "@/lib/api";


function cand(over: Partial<GoldenCandidate> = {}): GoldenCandidate {
  return {
    id: 1, run_id: "run_a", row_id: "r-1", project_id: "proj",
    promoted_by: "key_a", note: null, created_at: "2026-05-21T07:00:00",
    row_data: null,
    ...over,
  };
}


describe("filterAndSort", () => {
  const data = [
    cand({ id: 1, row_id: "refund-q", note: "refund flow", promoted_by: "key_alice",
           created_at: "2026-05-21T07:00:00" }),
    cand({ id: 2, row_id: "greeting", note: null, promoted_by: "key_bob",
           created_at: "2026-05-22T07:00:00",
           row_data: { input: "hello there", output: "hi" } }),
    cand({ id: 3, row_id: "escalate", note: "angry customer", promoted_by: "key_alice",
           created_at: "2026-05-20T07:00:00" }),
  ];

  it("defaults to newest-first by created_at", () => {
    const out = filterAndSort(data, "", "when");
    expect(out.map((c) => c.id)).toEqual([2, 1, 3]);  // 05-22, 05-21, 05-20
  });

  it("sorts by reviewer alphabetically", () => {
    const out = filterAndSort(data, "", "reviewer");
    // alice, alice, bob — the two alices keep relative order (stable).
    expect(out.map((c) => c.promoted_by)).toEqual(["key_alice", "key_alice", "key_bob"]);
  });

  it("sorts by row_id alphabetically", () => {
    const out = filterAndSort(data, "", "row");
    expect(out.map((c) => c.row_id)).toEqual(["escalate", "greeting", "refund-q"]);
  });

  it("filters across row_id / note / reviewer", () => {
    expect(filterAndSort(data, "refund", "when").map((c) => c.id)).toEqual([1]);
    expect(filterAndSort(data, "angry", "when").map((c) => c.id)).toEqual([3]);
    expect(filterAndSort(data, "key_bob", "when").map((c) => c.id)).toEqual([2]);
  });

  it("filters across row CONTENT (input/output) when expanded", () => {
    // "hello" only appears in candidate 2's row_data.input — proving
    // the filter reaches into the expanded content, not just metadata.
    expect(filterAndSort(data, "hello", "when").map((c) => c.id)).toEqual([2]);
  });

  it("is case-insensitive and trims the needle", () => {
    expect(filterAndSort(data, "  REFUND  ", "when").map((c) => c.id)).toEqual([1]);
  });

  it("returns a copy — never mutates the input array order", () => {
    const original = [...data];
    filterAndSort(data, "", "row");
    expect(data.map((c) => c.id)).toEqual(original.map((c) => c.id));
  });

  it("empty filter returns all rows", () => {
    expect(filterAndSort(data, "", "when")).toHaveLength(3);
  });
});
