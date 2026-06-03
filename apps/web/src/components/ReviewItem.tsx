/**
 * One queue item with its verdict form. Lives in its own file so
 * the rendering + form-state logic is unit-testable without the
 * route-level data-fetching scaffolding.
 *
 * No data-fetching here — the parent owns the queue query and the
 * submit mutation; this component just renders + emits ``onSubmit``.
 */

"use client";

import { useState } from "react";

import { Badge } from "./Badge";
import type { ReviewQueueItem, ReviewVerdict } from "@/lib/api";


const VERDICTS: { value: ReviewVerdict; label: string; tone: "pass" | "warn" | "fail" | "muted" }[] = [
  // Order intentional: positive ("automated was right") first so a
  // reviewer who agrees can submit with one click on the leftmost
  // option. Skip is rightmost so it doesn't get hit by accident.
  { value: "agree",          label: "Agree (fail)",     tone: "muted" },
  { value: "override_pass",  label: "Override → pass",  tone: "pass"  },
  { value: "override_fail",  label: "Override → fail",  tone: "fail"  },
  { value: "skip",           label: "Skip",             tone: "warn"  },
];


export function ReviewItem({
  item,
  onSubmit,
  submitting = false,
}: {
  item: ReviewQueueItem;
  onSubmit: (verdict: ReviewVerdict, note: string) => void;
  submitting?: boolean;
}) {
  const [verdict, setVerdict] = useState<ReviewVerdict | null>(null);
  const [note, setNote]       = useState<string>("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!verdict || submitting) return;
    onSubmit(verdict, note.trim());
  }

  return (
    <div
      data-testid="review-item"
      data-row-id={item.row_id}
      className="rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] p-3"
    >
      <div className="flex flex-wrap items-baseline gap-3 text-sm">
        <span className="font-mono text-xs">{item.row_id}</span>
        <Badge tone="fail">automated: fail</Badge>
        <span className="text-xs text-[var(--color-fg-muted)]">
          {item.trial_id}
        </span>
        <span className="ml-auto text-xs text-[var(--color-fg-muted)]">
          ${item.cost_usd.toFixed(4)} · {item.latency_ms} ms
        </span>
      </div>

      {item.failing_gates.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1 text-xs">
          <span className="text-[var(--color-fg-muted)]">failing gates:</span>
          {item.failing_gates.map((g) => (
            <Badge key={g} tone="fail">
              <span className="font-mono">{g}</span>
            </Badge>
          ))}
        </div>
      )}

      {item.tags.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1 text-xs">
          <span className="text-[var(--color-fg-muted)]">tags:</span>
          {item.tags.map((t) => (
            <Badge key={t} tone="muted">{t}</Badge>
          ))}
        </div>
      )}

      <form onSubmit={submit} className="mt-3 space-y-2">
        <div className="flex flex-wrap gap-2">
          {VERDICTS.map((v) => (
            <button
              key={v.value}
              type="button"
              data-verdict={v.value}
              aria-pressed={verdict === v.value}
              onClick={() => setVerdict(v.value)}
              className={
                "rounded border px-3 py-1 text-xs " +
                (verdict === v.value
                  ? "border-[var(--color-accent)] bg-[var(--color-bg-row)]"
                  : "border-[var(--color-border)] hover:bg-[var(--color-bg-row)]")
              }
            >
              {v.label}
            </button>
          ))}
        </div>
        <textarea
          name="note"
          placeholder="Optional note (visible to other reviewers)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={4000}
          rows={2}
          className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 text-xs"
        />
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={!verdict || submitting}
            className={
              "rounded border px-3 py-1 text-xs " +
              (!verdict || submitting
                ? "cursor-not-allowed border-[var(--color-border)] text-[var(--color-fg-muted)]"
                : "border-[var(--color-accent)] hover:bg-[var(--color-bg-row)]")
            }
          >
            {submitting ? "Submitting…" : "Submit review"}
          </button>
        </div>
      </form>
    </div>
  );
}
