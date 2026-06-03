"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge, statusTone } from "@/components/Badge";
import { ConnectionGate } from "@/components/ConnectionGate";
import { DriftBody } from "@/components/DriftBody";
import {
  getRun, getRunDrift,
  type Gate, type RunOut, type Trial,
} from "@/lib/api";

/**
 * Detail page reads ``?id=run_xxx`` from the query string instead
 * of using a ``[run_id]`` dynamic segment.  Reason: static export
 * can't serve unknown dynamic segments without listing them all
 * at build time, and listing every run_id at build time defeats
 * the SPA model.  Query-string params are static-export friendly —
 * the page is a single ``runs/detail/index.html`` that runs on the
 * client.
 */
export default function RunDetailPage() {
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
  const runId = params.get("id");
  if (!runId) {
    return (
      <Card title="Missing run id">
        <p className="text-sm text-[var(--color-fg-muted)]">
          The detail page needs an
          <code className="mx-1 rounded bg-[var(--color-bg-row)] px-1 py-0.5">?id=run_xxx</code>
          query parameter.
        </p>
        <Link
          href="/runs"
          className="mt-3 inline-block rounded border border-[var(--color-border)] px-3 py-1.5 text-sm hover:bg-[var(--color-bg-row)]"
        >
          ← Back to runs
        </Link>
      </Card>
    );
  }
  return <RunDetail runId={runId} />;
}


function RunDetail({ runId }: { runId: string }) {
  const params = useSearchParams();
  const baselineId = params.get("baseline");
  const q = useQuery({
    queryKey: ["run", runId],
    queryFn: () => getRun(runId),
  });

  if (q.isPending) return <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>;
  if (q.error)
    return (
      <p className="text-sm text-[var(--color-fail)]">
        {q.error instanceof Error ? q.error.message : String(q.error)}
      </p>
    );
  if (!q.data) return null;
  const run = q.data;

  return (
    <div className="space-y-4">
      <Header run={run} />
      <SummaryGrid run={run} />
      <DriftSection runId={runId} baselineId={baselineId} />
      {run.aggregate?.gates && run.aggregate.gates.length > 0 && (
        <Card title="Aggregate gates">
          <GatesTable gates={run.aggregate.gates} />
        </Card>
      )}
      <Card title="Trials">
        <TrialsList trials={run.trials} />
      </Card>
      {run.assets && run.assets.length > 0 && (
        <Card title="Assets">
          <AssetsTable assets={run.assets} />
        </Card>
      )}
    </div>
  );
}


/**
 * Drift card — read-only when ``?baseline=run_xxx`` is set, otherwise
 * shows an input that adds the param to the URL on submit. Pushing
 * to the router (rather than fetching directly with local state) so
 * the comparison is shareable / back-button-friendly.
 */
function DriftSection({
  runId,
  baselineId,
}: {
  runId: string;
  baselineId: string | null;
}) {
  const router  = useRouter();
  // Round-8 review-pass: controlled input so the "clear" link
  // ACTUALLY clears the field.  The previous ``defaultValue`` made
  // the input uncontrolled — Next's client-side navigation after
  // clicking "clear" stripped ``?baseline=`` from the URL but the
  // DOM input kept its stale value, and pressing Compare re-applied
  // the just-cleared baseline.  Sync ``draft`` to ``baselineId``
  // whenever the URL changes so external changes (clear link, back
  // button, paste-link) update the field.
  const [draft, setDraft] = useState<string>(baselineId ?? "");
  useEffect(() => {
    setDraft(baselineId ?? "");
  }, [baselineId]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const id = draft.trim();
    if (!id) return;
    // ``replace`` (not push) so the user's history isn't littered
    // with intermediate baselines as they iterate.
    router.replace(`/runs/detail/?id=${encodeURIComponent(runId)}&baseline=${encodeURIComponent(id)}`);
  }

  return (
    <Card title="Drift">
      <form onSubmit={submit} className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-[var(--color-fg-muted)]" htmlFor="drift-baseline">
          Compare with baseline run id
        </label>
        <input
          id="drift-baseline"
          name="baseline"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="run_xxxxxxxx"
          className="flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 font-mono text-xs"
        />
        <button
          type="submit"
          className="rounded border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-bg-row)]"
        >
          Compare
        </button>
        {baselineId && (
          <Link
            href={`/runs/detail/?id=${encodeURIComponent(runId)}`}
            className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
          >
            clear
          </Link>
        )}
      </form>
      {baselineId && <DriftReportView runId={runId} baselineId={baselineId} />}
    </Card>
  );
}


function DriftReportView({
  runId,
  baselineId,
}: {
  runId: string;
  baselineId: string;
}) {
  const q = useQuery({
    queryKey: ["drift", runId, baselineId],
    queryFn: () => getRunDrift(runId, baselineId),
    // Drift is a deterministic read; no need to refetch on focus.
    refetchOnWindowFocus: false,
  });
  if (q.isPending)
    return <p className="mt-3 text-sm text-[var(--color-fg-muted)]">Computing drift…</p>;
  if (q.error)
    return (
      <p className="mt-3 text-sm text-[var(--color-fail)]">
        {q.error instanceof Error ? q.error.message : String(q.error)}
      </p>
    );
  if (!q.data) return null;
  return <DriftBody report={q.data} />;
}




function Header({ run }: { run: RunOut }) {
  return (
    <div>
      <Link
        href="/runs"
        className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
      >
        ← Runs
      </Link>
      <div className="mt-1 flex flex-wrap items-baseline gap-3">
        <h1 className="font-mono text-lg font-semibold">{run.run_id}</h1>
        <Badge tone={statusTone(run.status)}>{run.status ?? "—"}</Badge>
        <Badge tone={statusTone(run.gate_status)}>gates: {run.gate_status ?? "—"}</Badge>
        <span className="text-sm text-[var(--color-fg-muted)]">
          project <span className="text-[var(--color-fg)]">{run.project}</span>
        </span>
        {run.server?.ingested_by && (
          <span
            className="text-xs text-[var(--color-fg-muted)]"
            title={run.server.ingested_at}
          >
            via <span className="font-mono">{run.server.ingested_by}</span>
          </span>
        )}
      </div>
    </div>
  );
}


function SummaryGrid({ run }: { run: RunOut }) {
  const items: Array<[string, string]> = [
    ["Rows", `${run.row_pass_count ?? 0} / ${run.row_count ?? 0}`],
    ["Failures", String(run.row_fail_count ?? 0)],
    ["Cost", `$${(run.cost_usd ?? 0).toFixed(4)}`],
    ["Trials", String(run.trials.length)],
    ["Schema", run.schema_version],
  ];
  if (run.config_hash) items.push(["Config hash", run.config_hash.slice(0, 12) + "…"]);
  if (run.started_at) items.push(["Started", fmtTime(run.started_at)]);
  if (run.finished_at) items.push(["Finished", fmtTime(run.finished_at)]);

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {items.map(([k, v]) => (
        <div
          key={k}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] px-3 py-2"
        >
          <div className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
            {k}
          </div>
          <div className="mt-0.5 truncate font-mono text-sm" title={v}>
            {v}
          </div>
        </div>
      ))}
    </div>
  );
}


function TrialsList({ trials }: { trials: Trial[] }) {
  if (trials.length === 0)
    return <p className="text-sm text-[var(--color-fg-muted)]">No trials.</p>;
  return (
    <div className="space-y-4">
      {trials.map((t) => (
        <div
          key={t.trial_id}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-row)] p-3"
        >
          <div className="flex flex-wrap items-baseline gap-3">
            <span className="font-mono text-xs">{t.trial_id}</span>
            <span className="text-sm">
              {t.provider}:{t.model}
            </span>
            <Badge tone={statusTone(t.status)}>{t.status ?? "—"}</Badge>
            <Badge tone={statusTone(t.gate_status)}>gates: {t.gate_status ?? "—"}</Badge>
            <span className="ml-auto text-xs text-[var(--color-fg-muted)]">
              {t.row_pass_count}/{t.row_count} rows · ${t.cost_usd.toFixed(4)}
            </span>
          </div>
          {t.gates.length > 0 && (
            <div className="mt-3">
              <GatesTable gates={t.gates} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}


function GatesTable({ gates }: { gates: Gate[] }) {
  return (
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        <tr className="border-b border-[var(--color-border)]">
          <th className="px-2 py-1.5 text-left font-medium">Gate</th>
          <th className="px-2 py-1.5 text-left font-medium">Severity</th>
          <th className="px-2 py-1.5 text-left font-medium">Layer</th>
          <th className="px-2 py-1.5 text-left font-medium">Result</th>
          <th className="px-2 py-1.5 text-left font-medium">Details</th>
        </tr>
      </thead>
      <tbody>
        {gates.map((g, i) => (
          <tr key={`${g.gate_name}-${i}`} className="border-b border-[var(--color-border)]">
            <td className="px-2 py-1.5 font-mono text-xs">{g.gate_name}</td>
            <td className="px-2 py-1.5">
              <Badge
                tone={
                  g.severity === "block" ? "fail" : g.severity === "warn" ? "warn" : "muted"
                }
              >
                {g.severity}
              </Badge>
            </td>
            <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
              {g.layer ?? "—"}
            </td>
            <td className="px-2 py-1.5">
              <Badge tone={g.passed ? "pass" : "fail"}>{g.passed ? "PASS" : "FAIL"}</Badge>
            </td>
            <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
              {g.details.length > 0 ? `${g.details.length} rule(s)` : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}


function AssetsTable({
  assets,
}: {
  assets: NonNullable<RunOut["assets"]>;
}) {
  return (
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        <tr className="border-b border-[var(--color-border)]">
          <th className="px-2 py-1.5 text-left font-medium">Kind</th>
          <th className="px-2 py-1.5 text-left font-medium">Asset id</th>
          <th className="px-2 py-1.5 text-left font-medium">Version</th>
          <th className="px-2 py-1.5 text-left font-medium">Source</th>
        </tr>
      </thead>
      <tbody>
        {assets.map((a, i) => (
          <tr key={`${a.asset_id}-${i}`} className="border-b border-[var(--color-border)]">
            <td className="px-2 py-1.5"><Badge tone="muted">{a.kind}</Badge></td>
            <td className="px-2 py-1.5 font-mono text-xs">{a.asset_id}</td>
            <td className="px-2 py-1.5 font-mono text-xs">
              {a.version_id.slice(0, 12)}…
            </td>
            <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
              {a.source ?? "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}


function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
