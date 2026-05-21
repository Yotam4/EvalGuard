"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge, statusTone } from "@/components/Badge";
import { ConnectionGate } from "@/components/ConnectionGate";
import { Tabs, type TabSpec } from "@/components/Tabs";
import { listRuns, type RunSource, type RunSummary } from "@/lib/api";


// 3-state filter for ``?source=``.  ``null`` means "no filter";
// ``cli`` and ``otlp`` are the canonical values the server's
// ``_KNOWN_SOURCES`` whitelist accepts.
type SourceFilter = null | RunSource;


export default function RunsPage() {
  return (
    <ConnectionGate>
      <Suspense fallback={<p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}>
        <RunsList />
      </Suspense>
    </ConnectionGate>
  );
}

function RunsList() {
  const router = useRouter();
  const params = useSearchParams();
  // ``source`` lives on the URL so the tab is shareable / bookmarkable
  // and the React Query cache key picks it up naturally.  Unknown
  // ``?source=`` values fall back to "all" rather than erroring
  // client-side — the server would reject anyway, but the UI stays
  // usable.
  const rawSource = params.get("source");
  const source: SourceFilter =
    rawSource === "cli" || rawSource === "otlp" ? rawSource : null;

  const [project, setProject] = useState("");

  const q = useQuery({
    queryKey: ["runs", project, source],
    queryFn: () => listRuns({
      project: project || undefined,
      source:  source ?? undefined,
      limit:   50,
    }),
    // Light polling so freshly-pushed runs appear without a manual
    // refresh. 10 s is generous — the underlying GET is cheap and
    // the server's QueryPool serves it from its hot path. React
    // Query dedupes the request with anything already in-flight.
    refetchInterval: 10_000,
  });

  function setSource(next: SourceFilter) {
    const qs = new URLSearchParams(params);
    if (next === null) qs.delete("source");
    else qs.set("source", next);
    // ``replace`` (not push) so the user's history isn't littered
    // with intermediate filter states as they click between tabs.
    router.replace(qs.toString() ? `/runs/?${qs}` : "/runs/");
  }

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

      <SourceTabs value={source} onChange={setSource} />

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


function SourceTabs({
  value, onChange,
}: { value: SourceFilter; onChange: (v: SourceFilter) => void }) {
  // Three pills.  ``null`` (all) is the default and the leftmost
  // — most operators want "everything" most of the time.  Keep the
  // order stable so muscle memory works.
  const tabs: TabSpec<SourceFilter>[] = [
    { value: null,   label: "All",         dataAttr: "all" },
    { value: "cli",  label: "CLI push",    dataAttr: "cli" },
    { value: "otlp", label: "OTLP traces", dataAttr: "otlp" },
  ];
  return (
    <Tabs
      value={value}
      onChange={onChange}
      tabs={tabs}
      ariaLabel="Filter runs by source"
      testid="source-tabs"
      dataAttrName="source-tab"
    />
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
              // ``data-testid`` + ``data-run-id`` so Playwright (and
              // any future e2e) can locate a specific row by id
              // rather than text-matching against the run_id link
              // — text matching breaks the moment two runs share a
              // prefix or the column reorders.
              data-testid="run-row"
              data-run-id={r.run_id}
              data-source={r.source}
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
