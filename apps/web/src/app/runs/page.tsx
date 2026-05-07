"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge, statusTone } from "@/components/Badge";
import { ConnectionGate } from "@/components/ConnectionGate";
import { listRuns, type RunSummary } from "@/lib/api";

export default function RunsPage() {
  return (
    <ConnectionGate>
      <RunsList />
    </ConnectionGate>
  );
}

function RunsList() {
  const [project, setProject] = useState("");

  const q = useQuery({
    queryKey: ["runs", project],
    queryFn: () => listRuns({ project: project || undefined, limit: 50 }),
    // Light polling so freshly-pushed runs appear without a manual
    // refresh. 10 s is generous — the underlying GET is cheap and
    // the server's QueryPool serves it from its hot path. React
    // Query dedupes the request with anything already in-flight.
    refetchInterval: 10_000,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-end gap-3">
        <h1 className="text-xl font-semibold">Runs</h1>
        <input
          value={project}
          onChange={(e) => setProject(e.target.value)}
          placeholder="filter by project"
          className="ml-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
        />
      </div>

      <Card>
        {q.isPending && <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}
        {q.error && (
          <p className="text-sm text-[var(--color-fail)]">
            {q.error instanceof Error ? q.error.message : String(q.error)}
          </p>
        )}
        {q.data && <RunsTable runs={q.data.runs} />}
      </Card>
    </div>
  );
}


function RunsTable({ runs }: { runs: RunSummary[] }) {
  if (runs.length === 0) {
    return (
      <p className="text-sm text-[var(--color-fg-muted)]">
        No runs yet. Push one with <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">evalguard push --last</code>.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
          <tr className="border-b border-[var(--color-border)]">
            <Th>Run</Th>
            <Th>Project</Th>
            <Th>Status</Th>
            <Th>Gates</Th>
            <Th align="right">Pass / total</Th>
            <Th align="right">Cost</Th>
            <Th>Source</Th>
            <Th>Ingested</Th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr
              key={r.run_id}
              className="border-b border-[var(--color-border)] hover:bg-[var(--color-bg-row)]"
            >
              <Td>
                <Link
                  href={`/runs/detail/?id=${encodeURIComponent(r.run_id)}`}
                  className="font-mono text-xs text-[var(--color-accent)] hover:underline"
                >
                  {r.run_id}
                </Link>
              </Td>
              <Td>{r.project}</Td>
              <Td><Badge tone={statusTone(r.status)}>{r.status ?? "—"}</Badge></Td>
              <Td><Badge tone={statusTone(r.gate_status)}>{r.gate_status ?? "—"}</Badge></Td>
              <Td align="right">
                <span className="font-mono text-xs">
                  {r.row_pass_count}/{r.row_count}
                </span>
              </Td>
              <Td align="right">
                <span className="font-mono text-xs">${r.cost_usd.toFixed(4)}</span>
              </Td>
              <Td>
                <Badge tone={r.source === "otlp" ? "info" : "muted"}>
                  {r.source}
                </Badge>
              </Td>
              <Td>
                <span className="text-xs text-[var(--color-fg-muted)]" title={r.ingested_by ?? ""}>
                  {fmtTime(r.ingested_at)}
                </span>
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function Th({ children, align = "left" }: { children: React.ReactNode; align?: "left" | "right" }) {
  return (
    <th
      className={
        "px-3 py-2 font-medium " + (align === "right" ? "text-right" : "text-left")
      }
    >
      {children}
    </th>
  );
}

function Td({ children, align = "left" }: { children: React.ReactNode; align?: "left" | "right" }) {
  return (
    <td className={"px-3 py-2 " + (align === "right" ? "text-right" : "text-left")}>
      {children}
    </td>
  );
}


function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
