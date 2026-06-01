/**
 * One row of the calls-stream view.  Pure presentational — takes a
 * ``CallSummary`` + an ``onSelect`` callback, emits no fetches.
 *
 * Lives in its own file so the rendering logic (output-preview
 * truncation, pass/fail tone, cost/latency formatting) is unit-
 * testable without React-Query / virtualizer scaffolding.  Same
 * convention as ``DriftBody``, ``ReviewItem``, ``AssetVersionsTable``.
 */

"use client";

import { Badge } from "./Badge";
import type { CallSummary } from "@/lib/api";


export function CallCard({
  call,
  selected = false,
  onSelect,
}: {
  call: CallSummary;
  selected?: boolean;
  onSelect: (call: CallSummary) => void;
}) {
  return (
    <button
      type="button"
      data-testid="call-card"
      data-run-id={call.run_id}
      data-row-id={call.row_id}
      data-passed={call.passed ? "true" : "false"}
      aria-pressed={selected}
      onClick={() => onSelect(call)}
      className={
        "block w-full rounded border bg-[var(--color-bg-card)] p-3 text-left transition " +
        (selected
          ? "border-[var(--color-accent)] ring-1 ring-[var(--color-accent)]"
          : "border-[var(--color-border)] hover:bg-[var(--color-bg-row)]")
      }
    >
      <div className="flex flex-wrap items-baseline gap-2 text-sm">
        <Badge tone={call.passed ? "pass" : "fail"}>
          {call.passed ? "PASS" : "FAIL"}
        </Badge>
        <span className="font-mono text-xs text-[var(--color-fg-muted)]">
          {call.row_id}
        </span>
        {/* PROXY-2.5 review-pass: a "live" badge on proxied rows so
            operators scanning /calls/ can tell production traffic
            apart from CI-pushed batch rows at a glance.  Batch
            sources (cli / otlp) stay unbadged — they were the
            default before the proxy shipped. */}
        {call.source === "live" && <Badge tone="info">live</Badge>}
        {call.cache_hit && <Badge tone="muted">cache</Badge>}
        {call.tags.slice(0, 3).map((t) => (
          <Badge key={t} tone="info">{t}</Badge>
        ))}
        <span className="ml-auto flex items-center gap-3 text-xs text-[var(--color-fg-muted)]">
          <span className="font-mono">{call.latency_ms} ms</span>
          <span className="font-mono">${call.cost_usd.toFixed(4)}</span>
          {call.ingested_at && (
            <span title={call.ingested_at}>{fmtTime(call.ingested_at)}</span>
          )}
        </span>
      </div>
      {call.output_preview && (
        <p
          className="mt-2 line-clamp-2 text-xs text-[var(--color-fg)]"
          title={call.output_preview}
        >
          {call.output_preview}
        </p>
      )}
    </button>
  );
}


function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
