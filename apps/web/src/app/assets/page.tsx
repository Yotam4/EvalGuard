"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { ConnectionGate } from "@/components/ConnectionGate";
import { listAssets, type AssetKind, type AssetSummary } from "@/lib/api";

/**
 * /assets — cross-run aggregation. One row per (project, kind,
 * asset_id) tuple, with version count and run count.
 *
 * The kind picker is intentionally a row of tabs, not a dropdown:
 * the seven kinds are stable and visible-at-a-glance is more
 * important than chrome economy.
 *
 * Query string drives the kind so deep-links (``/assets/?kind=judge``)
 * work and the back button restores state.
 */
export default function AssetsPage() {
  return (
    <ConnectionGate>
      <Suspense fallback={<p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}>
        <Inner />
      </Suspense>
    </ConnectionGate>
  );
}


const KINDS: { value: AssetKind; label: string }[] = [
  { value: "prompt",    label: "Prompts" },
  { value: "dataset",   label: "Datasets" },
  { value: "judge",     label: "Judges" },
  { value: "heuristic", label: "Heuristics" },
  { value: "metric",    label: "Metrics" },
  { value: "schema",    label: "Schemas" },
  { value: "rubric",    label: "Rubrics" },
];


function Inner() {
  const params = useSearchParams();
  const initialKind = (params.get("kind") as AssetKind | null) ?? "dataset";
  const [kind, setKind] = useState<AssetKind>(initialKind);
  const [project, setProject] = useState("");

  const q = useQuery({
    queryKey: ["assets", kind, project || null],
    queryFn:  () => listAssets({ kind, project: project || undefined, limit: 200 }),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <h1 className="text-xl font-semibold">Assets</h1>
        <input
          value={project}
          onChange={(e) => setProject(e.target.value)}
          placeholder="filter by project"
          className="ml-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
        />
      </div>

      <KindTabs value={kind} onChange={setKind} />

      <Card>
        {q.isPending && <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}
        {q.error && (
          <p className="text-sm text-[var(--color-fail)]">
            {q.error instanceof Error ? q.error.message : String(q.error)}
          </p>
        )}
        {q.data && <AssetsTable kind={kind} assets={q.data.assets} />}
      </Card>
    </div>
  );
}


function KindTabs({
  value, onChange,
}: { value: AssetKind; onChange: (k: AssetKind) => void }) {
  return (
    <div className="flex flex-wrap gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] p-1">
      {KINDS.map((k) => {
        const active = k.value === value;
        return (
          <button
            key={k.value}
            type="button"
            onClick={() => onChange(k.value)}
            className={
              "rounded px-3 py-1.5 text-sm transition " +
              (active
                ? "bg-[var(--color-bg-row)] text-[var(--color-fg)]"
                : "text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)] hover:text-[var(--color-fg)]")
            }
          >
            {k.label}
          </button>
        );
      })}
    </div>
  );
}


function AssetsTable({
  kind, assets,
}: { kind: AssetKind; assets: AssetSummary[] }) {
  if (assets.length === 0) {
    return (
      <p className="text-sm text-[var(--color-fg-muted)]">
        No <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">{kind}</code> assets yet.
        They show up here after the first run that loads one is pushed.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
          <tr className="border-b border-[var(--color-border)]">
            <th className="px-3 py-2 text-left font-medium">Asset id</th>
            <th className="px-3 py-2 text-left font-medium">Project</th>
            <th className="px-3 py-2 text-right font-medium">Versions</th>
            <th className="px-3 py-2 text-right font-medium">Runs</th>
            <th className="px-3 py-2 text-left font-medium">Last seen</th>
            <th className="px-3 py-2 text-left font-medium">Last version</th>
            <th className="px-3 py-2 text-left font-medium">Last run</th>
          </tr>
        </thead>
        <tbody>
          {assets.map((a) => (
            <tr
              key={`${a.project_id}-${a.kind}-${a.asset_id}`}
              className="border-b border-[var(--color-border)] hover:bg-[var(--color-bg-row)]"
            >
              <td className="px-3 py-2 font-mono text-xs">
                <Link
                  href={`/assets/detail/?kind=${encodeURIComponent(a.kind)}`
                       + `&asset_id=${encodeURIComponent(a.asset_id)}`
                       + `&project_id=${encodeURIComponent(a.project_id)}`}
                  className="text-[var(--color-accent)] hover:underline"
                >
                  {a.asset_id}
                </Link>
              </td>
              <td className="px-3 py-2">
                <Link
                  href={`/runs/?project=${encodeURIComponent(a.project_name)}`}
                  className="text-xs text-[var(--color-accent)] hover:underline"
                >
                  {a.project_name}
                </Link>
              </td>
              <td className="px-3 py-2 text-right">
                <Badge tone={a.version_count === 1 ? "muted" : "info"}>
                  {a.version_count}
                </Badge>
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs">
                {a.run_count}
              </td>
              <td className="px-3 py-2 text-xs text-[var(--color-fg-muted)]">
                {fmtTime(a.last_seen)}
              </td>
              <td className="px-3 py-2 font-mono text-xs">
                {a.last_version_id.slice(0, 12)}…
              </td>
              <td className="px-3 py-2">
                <Link
                  href={`/runs/detail/?id=${encodeURIComponent(a.last_run_id)}`}
                  className="font-mono text-xs text-[var(--color-accent)] hover:underline"
                >
                  {a.last_run_id.slice(0, 12)}…
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
