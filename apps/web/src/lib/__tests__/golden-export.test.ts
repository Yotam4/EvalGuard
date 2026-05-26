import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import {
  candidateToJsonlRow, composeJsonl, triggerDownload,
} from "../golden-export";
import type { GoldenCandidate } from "../api";


function cand(overrides: Partial<GoldenCandidate> = {}): GoldenCandidate {
  return {
    id: 1,
    run_id: "run_a",
    row_id: "r-1",
    project_id: "proj",
    promoted_by: "key_a",
    note: "looks good",
    created_at: "2026-05-21T07:30:00",
    row_data: { input: "What is X?", expected: "X is …", output: "X is foo" },
    ...overrides,
  };
}


describe("candidateToJsonlRow", () => {
  it("emits keys in lexicographic order to match the CLI's sort_keys", () => {
    const line = candidateToJsonlRow(cand());
    expect(line).not.toBeNull();
    // Top-level key order must be _provenance, expected, id, input —
    // the same order the Python CLI's json.dumps(sort_keys=True)
    // produces, so a browser-exported file diffs cleanly against a
    // CLI-exported one.
    const keysInOrder = [...(line as string).matchAll(/"(_provenance|expected|id|input)":/g)]
      .map((m) => m[1]);
    expect(keysInOrder).toEqual(["_provenance", "expected", "id", "input"]);
  });

  it("attaches provenance from the candidate metadata", () => {
    const parsed = JSON.parse(candidateToJsonlRow(cand()) as string);
    expect(parsed.id).toBe("r-1");
    expect(parsed.input).toBe("What is X?");
    expect(parsed._provenance).toEqual({
      created_at: "2026-05-21T07:30:00",
      note: "looks good",
      promoted_by: "key_a",
      run_id: "run_a",
    });
  });

  it("returns null when row_data is absent (not expanded)", () => {
    expect(candidateToJsonlRow(cand({ row_data: null }))).toBeNull();
    expect(candidateToJsonlRow(cand({ row_data: undefined }))).toBeNull();
  });

  it("returns null when input is null/undefined (CLI skip rule)", () => {
    expect(candidateToJsonlRow(cand({ row_data: { input: null } }))).toBeNull();
    expect(candidateToJsonlRow(cand({ row_data: { input: undefined } }))).toBeNull();
  });

  it("preserves a structured (non-string) input", () => {
    const parsed = JSON.parse(
      candidateToJsonlRow(cand({ row_data: { input: { q: "x", k: 3 } } })) as string,
    );
    expect(parsed.input).toEqual({ q: "x", k: 3 });
  });

  it("expected defaults to null when absent", () => {
    const parsed = JSON.parse(
      candidateToJsonlRow(cand({ row_data: { input: "hi" } })) as string,
    );
    expect(parsed.expected).toBeNull();
  });
});


describe("composeJsonl", () => {
  it("joins exportable rows with a trailing newline + reports counts", () => {
    const res = composeJsonl([
      cand({ id: 1, row_id: "r-1" }),
      cand({ id: 2, row_id: "r-2", row_data: { input: null } }),  // skipped
      cand({ id: 3, row_id: "r-3" }),
    ]);
    expect(res.exportedCount).toBe(2);
    expect(res.skippedNoInput).toBe(1);
    expect(res.jsonl.endsWith("\n")).toBe(true);
    expect(res.jsonl.trimEnd().split("\n")).toHaveLength(2);
  });

  it("produces an empty string (not a lone newline) when nothing exports", () => {
    const res = composeJsonl([cand({ row_data: null })]);
    expect(res.jsonl).toBe("");
    expect(res.exportedCount).toBe(0);
    expect(res.skippedNoInput).toBe(1);
  });
});


describe("triggerDownload", () => {
  let clickSpy: ReturnType<typeof vi.fn>;
  let createdUrl: string | null;

  beforeEach(() => {
    createdUrl = null;
    clickSpy = vi.fn();
    // Stub the URL object-url API (happy-dom doesn't implement it).
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => { createdUrl = "blob:fake"; return createdUrl; }),
      revokeObjectURL: vi.fn(),
    });
    // Intercept the anchor click so the test doesn't actually
    // navigate.
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => {
      const el = Object.assign(document.createElementNS("http://www.w3.org/1999/xhtml", tag), {});
      if (tag === "a") (el as HTMLAnchorElement).click = clickSpy;
      return el;
    }) as typeof document.createElement);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("creates a blob URL and clicks an anchor", () => {
    triggerDownload("out.jsonl", '{"id":"r-1"}\n');
    expect(createdUrl).toBe("blob:fake");
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });
});
