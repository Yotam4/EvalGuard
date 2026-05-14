"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { ConnectionGate } from "@/components/ConnectionGate";
import { ReviewItem } from "@/components/ReviewItem";
import {
  getReviewQueue, listRunReviews, submitReview,
  type Review, type ReviewVerdict,
} from "@/lib/api";


/**
 * Phase 4 — human review queue.
 *
 * URL shape: ``/reviews?run_id=run_xxx``. The run id lives on the
 * URL (not local state) so the page is shareable / bookmarkable
 * and React Query's cache key includes it naturally.
 *
 * When ``run_id`` is missing, we render a small prompt to pick a
 * run rather than a generic "no data" view — the queue concept
 * doesn't exist outside a specific run.
 */
export default function ReviewsPage() {
  return (
    <ConnectionGate>
      <Suspense fallback={<p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}>
        <Inner />
      </Suspense>
    </ConnectionGate>
  );
}


function Inner() {
  const router = useRouter();
  const params = useSearchParams();
  const runId  = params.get("run_id");

  function pickRun(e: React.FormEvent) {
    e.preventDefault();
    const data = new FormData(e.currentTarget as HTMLFormElement);
    const id = String(data.get("run_id") ?? "").trim();
    if (id) router.replace(`/reviews?run_id=${encodeURIComponent(id)}`);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Reviews</h1>
        <p className="text-sm text-[var(--color-fg-muted)]">
          Human review queue for automated failures.
        </p>
      </div>

      <Card title="Run">
        <form onSubmit={pickRun} className="flex flex-wrap items-center gap-2">
          <label htmlFor="run_id" className="text-xs text-[var(--color-fg-muted)]">
            run id
          </label>
          <input
            id="run_id" name="run_id"
            defaultValue={runId ?? ""}
            placeholder="run_xxxxxxxx"
            className="flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] px-2 py-1 font-mono text-xs"
          />
          <button
            type="submit"
            className="rounded border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-bg-row)]"
          >
            Load queue
          </button>
          {runId && (
            <Link
              href={`/runs/detail/?id=${encodeURIComponent(runId)}`}
              className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
            >
              open run →
            </Link>
          )}
        </form>
      </Card>

      {runId && <ReviewBody runId={runId} />}
    </div>
  );
}


function ReviewBody({ runId }: { runId: string }) {
  const qc = useQueryClient();

  const queue = useQuery({
    queryKey: ["reviews", "queue", runId],
    queryFn: () => getReviewQueue(runId),
    refetchOnWindowFocus: false,
  });

  const existing = useQuery({
    queryKey: ["reviews", "list", runId],
    queryFn: () => listRunReviews(runId),
    refetchOnWindowFocus: false,
  });

  const submitter = useMutation({
    mutationFn: (body: { row_id: string; verdict: ReviewVerdict; note: string }) =>
      submitReview({
        run_id:  runId,
        row_id:  body.row_id,
        verdict: body.verdict,
        // Server normalises empty strings → null; sending the empty
        // explicitly is fine too.
        note:    body.note || undefined,
      }),
    onSuccess: () => {
      // Re-fetch both queue (the reviewed row drops out) and reviews
      // list (the new entry shows up).
      qc.invalidateQueries({ queryKey: ["reviews", "queue", runId] });
      qc.invalidateQueries({ queryKey: ["reviews", "list",  runId] });
    },
  });

  if (queue.isPending || existing.isPending)
    return <p className="text-sm text-[var(--color-fg-muted)]">Loading queue…</p>;

  // Surface BOTH errors — previously only ``queue.error`` was
  // rendered, so a 403 / 404 / timeout on ``listRunReviews`` would
  // silently leave the reviewer staring at an empty "Existing
  // reviews" section with no idea why. The single rendered banner
  // takes whichever error fired first so a cascading failure
  // doesn't double-stack the same message.
  const err = queue.error ?? existing.error;
  if (err)
    return (
      <p className="text-sm text-[var(--color-fail)]">
        {err instanceof Error ? err.message : String(err)}
      </p>
    );

  const items   = queue.data?.items ?? [];
  const reviews = existing.data?.reviews ?? [];

  return (
    <div className="space-y-4">
      <Card title={`Queue (${items.length})`}>
        {items.length === 0 ? (
          <p className="text-sm text-[var(--color-fg-muted)]">
            Nothing waiting. Every failing row on this run has at
            least one review from you.
          </p>
        ) : (
          <div className="space-y-3">
            {items.map((it) => (
              <ReviewItem
                key={it.row_id}
                item={it}
                submitting={submitter.isPending}
                onSubmit={(verdict, note) =>
                  submitter.mutate({ row_id: it.row_id, verdict, note })
                }
              />
            ))}
          </div>
        )}
        {submitter.error && (
          <p className="mt-3 text-xs text-[var(--color-fail)]">
            Submit failed:{" "}
            {submitter.error instanceof Error
              ? submitter.error.message
              : String(submitter.error)}
          </p>
        )}
      </Card>

      <Card title={`Reviews on this run (${reviews.length})`}>
        {reviews.length === 0 ? (
          <p className="text-sm text-[var(--color-fg-muted)]">No reviews yet.</p>
        ) : (
          <ReviewsTable reviews={reviews} />
        )}
      </Card>
    </div>
  );
}


function ReviewsTable({ reviews }: { reviews: Review[] }) {
  return (
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        <tr className="border-b border-[var(--color-border)]">
          <th className="px-2 py-1.5 text-left font-medium">Row</th>
          <th className="px-2 py-1.5 text-left font-medium">Verdict</th>
          <th className="px-2 py-1.5 text-left font-medium">Reviewer</th>
          <th className="px-2 py-1.5 text-left font-medium">Note</th>
          <th className="px-2 py-1.5 text-left font-medium">Updated</th>
        </tr>
      </thead>
      <tbody>
        {reviews.map((r) => (
          <tr key={r.id} className="border-b border-[var(--color-border)]">
            <td className="px-2 py-1.5 font-mono text-xs">{r.row_id}</td>
            <td className="px-2 py-1.5">
              <Badge tone={verdictTone(r.verdict)}>{r.verdict}</Badge>
            </td>
            <td className="px-2 py-1.5 font-mono text-xs">{r.reviewer_key_id}</td>
            <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
              {r.note ?? "—"}
            </td>
            <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]" title={r.updated_at}>
              {new Date(r.updated_at).toLocaleString()}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}


function verdictTone(v: ReviewVerdict): "pass" | "warn" | "fail" | "muted" {
  switch (v) {
    case "agree":         return "muted";
    case "override_pass": return "pass";
    case "override_fail": return "fail";
    case "skip":          return "warn";
    default: {
      // Exhaustiveness check — if the server ships a new verdict
      // without the UI side being updated, TypeScript fails the
      // build here instead of silently returning ``undefined`` and
      // dropping the badge tone at runtime.
      const _exhaustive: never = v;
      throw new Error(`unhandled verdict: ${String(_exhaustive)}`);
    }
  }
}
