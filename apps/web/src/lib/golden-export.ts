/**
 * Client-side JSONL export for the golden-DB view.
 *
 * Produces byte-for-byte the same shape as the
 * ``evalguard golden export`` CLI subcommand
 * (``packages/cli/evalguard_cli/commands/golden_cmd.py``) so a row
 * exported from the browser and a row exported from the terminal
 * are interchangeable in a dataset file:
 *
 *   {"_provenance": {...}, "expected": ..., "id": ..., "input": ...}
 *
 * Keys are emitted in lexicographic order (matching the CLI's
 * ``json.dumps(..., sort_keys=True)``) so re-exports diff cleanly.
 * The whole module is pure (no DOM) except ``triggerDownload`` which
 * is the single browser-only seam — kept separate so the
 * composition logic is unit-testable under happy-dom.
 */

import type { GoldenCandidate } from "./api";


/**
 * One JSONL line for a candidate.  Returns ``null`` when the
 * candidate has no expanded ``row_data`` OR its ``input`` is
 * null/undefined — the same skip rule the CLI applies (a row with
 * ``"input": null`` would crash a downstream evaluator).
 */
export function candidateToJsonlRow(c: GoldenCandidate): string | null {
  const rd = c.row_data;
  if (!rd || rd.input === null || rd.input === undefined) return null;
  // Insert keys in sorted order so ``JSON.stringify`` (which
  // preserves insertion order) emits them the same way Python's
  // ``sort_keys=True`` does.  ``_`` sorts before letters in ASCII,
  // so ``_provenance`` leads.
  const obj = {
    _provenance: {
      created_at:  c.created_at,
      note:        c.note,
      promoted_by: c.promoted_by,
      run_id:      c.run_id,
    },
    expected: rd.expected ?? null,
    id:       c.row_id,
    input:    rd.input,
  };
  return JSON.stringify(obj);
}


export interface ComposeResult {
  jsonl:           string;
  exportedCount:   number;
  skippedNoInput:  number;
}


/** Compose a JSONL blob from the given candidates, skipping any
 *  that lack expanded input (reporting the skip count so the UI can
 *  surface it the way the CLI's stderr summary does). */
export function composeJsonl(candidates: GoldenCandidate[]): ComposeResult {
  const lines: string[] = [];
  let skipped = 0;
  for (const c of candidates) {
    const line = candidateToJsonlRow(c);
    if (line === null) { skipped += 1; continue; }
    lines.push(line);
  }
  return {
    jsonl:          lines.length ? lines.join("\n") + "\n" : "",
    exportedCount:  lines.length,
    skippedNoInput: skipped,
  };
}


/** Trigger a browser download of ``text`` as ``filename``.  The
 *  single DOM-touching function in this module; guarded so a
 *  non-browser caller (SSR, a test that forgot to stub) no-ops
 *  rather than throwing. */
export function triggerDownload(filename: string, text: string): void {
  if (typeof document === "undefined" || typeof URL.createObjectURL !== "function") {
    return;
  }
  const blob = new Blob([text], { type: "application/x-ndjson" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
