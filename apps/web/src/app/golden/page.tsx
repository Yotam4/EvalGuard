"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { ConnectionGate } from "@/components/ConnectionGate";
import { listGoldenCandidates, unPromoteGolden } from "@/lib/api";


/**
 * /golden/?project=customer-service — staged candidates list.
 *
 * Phase OBS-4.  Shows what's been promoted from the /calls/ stream
 * and lets the operator un-promote.  The actual export to JSONL
 * (writing the rows back to an on-disk dataset) is a future CLI
 * subcommand — this page is the audit trail of "what got promoted,
 * by whom, when".
 */
export default function GoldenPage() {
  return (
    <ConnectionGate>
      <Suspense fallback={<p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}>
        <Inner />
      </Suspense>
    </ConnectionGate>
  );
}


function Inner() {
  const params = useSearchParams();
  const project = params.get("project") ?? "";

  if (!project) {
    return (
      <Card title="Golden candidates">
        <p className="text-sm text-[var(--color-fg-muted)]">
          The staged-candidates list is per-project.  Pass{" "}
          <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">
            ?project=&lt;slug&gt;
          </code>{" "}
          on the URL.
        </p>
      </Card>
    );
  }
  return <Body projectSlug={project} />;
}


function Body({ projectSlug }: { projectSlug: string }) {
  const qc = useQueryClient();
  const q  = useQuery({
    queryKey: ["golden-candidates", projectSlug],
    queryFn: () => listGoldenCandidates(projectSlug),
    refetchOnWindowFocus: false,
  });

  const del = useMutation({
    mutationFn: (id: number) => unPromoteGolden(id),
    onSuccess: () => qc.invalidateQueries({
      queryKey: ["golden-candidates", projectSlug],
    }),
  });

  if (q.isPending)
    return <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>;
  if (q.error)
    return (
      <p className="text-sm text-[var(--color-fail)]">
        {q.error instanceof Error ? q.error.message : String(q.error)}
      </p>
    );

  const candidates = q.data?.candidates ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Golden candidates</h1>
        <span className="text-sm text-[var(--color-fg-muted)]">
          project <span className="text-[var(--color-fg)]">{projectSlug}</span>
        </span>
        <Link
          href={`/calls/?project=${encodeURIComponent(projectSlug)}`}
          className="ml-auto text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
        >
          ← back to calls
        </Link>
      </div>

      <Card>
        {candidates.length === 0 ? (
          <p className="text-sm text-[var(--color-fg-muted)]">
            No staged candidates yet.  Promote a call from the{" "}
            <Link
              href={`/calls/?project=${encodeURIComponent(projectSlug)}`}
              className="text-[var(--color-accent)] hover:underline"
            >
              calls stream
            </Link>
            .
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
              <tr className="border-b border-[var(--color-border)]">
                <th className="px-2 py-1.5 text-left font-medium">Row</th>
                <th className="px-2 py-1.5 text-left font-medium">Run</th>
                <th className="px-2 py-1.5 text-left font-medium">Promoted by</th>
                <th className="px-2 py-1.5 text-left font-medium">Note</th>
                <th className="px-2 py-1.5 text-left font-medium">When</th>
                <th className="px-2 py-1.5 text-right font-medium">Action</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c) => (
                <tr
                  key={c.id}
                  data-testid="golden-row"
                  data-candidate-id={c.id}
                  data-row-id={c.row_id}
                  className="border-b border-[var(--color-border)]"
                >
                  <td className="px-2 py-1.5 font-mono text-xs">
                    <Link
                      // Encode the composite ``run_id:row_id`` as one
                      // value, not each half separately — separately
                      // percent-encoding both produces ``a%3Aa%3Aroot``
                      // patterns that the consumer's ``indexOf(":")``
                      // splits on the FIRST ``%3A``, sending you to
                      // the wrong row when ``row_id`` contains a ``:``.
                      href={
                        `/calls/?project=${encodeURIComponent(projectSlug)}`
                        + `&call=${encodeURIComponent(`${c.run_id}:${c.row_id}`)}`
                      }
                      className="text-[var(--color-accent)] hover:underline"
                    >
                      {c.row_id}
                    </Link>
                  </td>
                  <td className="px-2 py-1.5 font-mono text-xs">{c.run_id}</td>
                  <td className="px-2 py-1.5 font-mono text-xs">{c.promoted_by}</td>
                  <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
                    {c.note ?? "—"}
                  </td>
                  <td
                    className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]"
                    title={c.created_at}
                  >
                    {fmtTime(c.created_at)}
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    <button
                      type="button"
                      disabled={del.isPending}
                      onClick={() => del.mutate(c.id)}
                      data-testid="golden-unpromote"
                      className="rounded border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)] disabled:opacity-40"
                    >
                      remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}


function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
