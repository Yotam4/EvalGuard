/**
 * Pure presentational view of a ``DriftReport``. Lives in its own
 * file (rather than inlined in ``runs/detail/page.tsx``) so the
 * formatting helpers and the verdict logic can be unit-tested
 * without React-Query / next-navigation scaffolding.
 *
 * No data-fetching here — the parent page owns the query + URL
 * state. This component just renders what it's given.
 */

import { Badge } from "./Badge";
import type { DriftMetric, DriftReport } from "@/lib/api";


export function DriftBody({ report }: { report: DriftReport }) {
  const sigCount = report.metrics.filter((m) => m.significant_at_alpha).length;
  return (
    <div className="mt-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-[var(--color-fg-muted)]">vs baseline</span>
        <span className="font-mono text-xs">{report.baseline_run_id}</span>
        <span className="text-[var(--color-fg-muted)]">at α =</span>
        <span className="font-mono text-xs">{report.alpha}</span>
        {sigCount > 0 ? (
          <Badge tone="fail">
            {sigCount} significant change{sigCount === 1 ? "" : "s"}
          </Badge>
        ) : (
          <Badge tone="pass">no significant drift</Badge>
        )}
      </div>
      {report.metrics.length > 0 && <DriftTable metrics={report.metrics} />}
      {report.skipped.length > 0 && (
        <div className="text-xs text-[var(--color-fg-muted)]" data-testid="drift-skipped">
          Skipped:{" "}
          {report.skipped.map((s, i) => (
            <span key={s.name}>
              {i > 0 && ", "}
              <span className="font-mono">{s.name}</span> ({s.reason})
            </span>
          ))}
        </div>
      )}
    </div>
  );
}


function DriftTable({ metrics }: { metrics: DriftMetric[] }) {
  return (
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        <tr className="border-b border-[var(--color-border)]">
          <th className="px-2 py-1.5 text-left font-medium">Metric</th>
          <th className="px-2 py-1.5 text-right font-medium">n (cur / base)</th>
          <th className="px-2 py-1.5 text-right font-medium">mean (cur / base)</th>
          <th className="px-2 py-1.5 text-right font-medium">Δ</th>
          <th className="px-2 py-1.5 text-right font-medium">p (two-sided)</th>
          <th className="px-2 py-1.5 text-left  font-medium">Verdict</th>
        </tr>
      </thead>
      <tbody>
        {metrics.map((m) => (
          <tr key={m.name} className="border-b border-[var(--color-border)]">
            <td className="px-2 py-1.5 font-mono text-xs">{m.name}</td>
            <td className="px-2 py-1.5 text-right font-mono text-xs">
              {m.n_current} / {m.n_baseline}
            </td>
            <td className="px-2 py-1.5 text-right font-mono text-xs">
              {fmtMean(m.mean_current)} / {fmtMean(m.mean_baseline)}
            </td>
            <td className="px-2 py-1.5 text-right font-mono text-xs">
              {fmtDelta(m.delta_mean)}
            </td>
            <td className="px-2 py-1.5 text-right font-mono text-xs">
              {fmtP(m.p_two_sided)}
            </td>
            <td className="px-2 py-1.5">
              {m.significant_at_alpha ? (
                <Badge tone={driftTone(m)}>{driftLabel(m)}</Badge>
              ) : (
                <Badge tone="muted">no signal</Badge>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}


// "lower is worse" for ``passed`` (pass-rate dropping is bad);
// "higher is worse" for ``latency_ms`` and ``cost_usd``. Map to the
// matching Badge tone so a meaningful regression renders red.
export function driftTone(m: DriftMetric): "fail" | "warn" {
  const lowerIsWorse = m.name === "passed";
  const regressed = lowerIsWorse ? m.delta_mean < 0 : m.delta_mean > 0;
  return regressed ? "fail" : "warn";
}


export function driftLabel(m: DriftMetric): string {
  const sign = m.delta_mean > 0 ? "↑" : "↓";
  return `${sign} significant`;
}


export function fmtMean(v: number): string {
  if (!isFinite(v)) return "—";
  if (Math.abs(v) >= 100) return v.toFixed(1);
  if (Math.abs(v) >= 1)   return v.toFixed(3);
  return v.toFixed(4);
}


export function fmtDelta(v: number): string {
  if (!isFinite(v)) return "—";
  const s = fmtMean(Math.abs(v));
  return v >= 0 ? `+${s}` : `−${s}`;
}


export function fmtP(p: number): string {
  if (!isFinite(p)) return "—";
  if (p < 1e-4) return "<1e-4";
  return p.toFixed(4);
}
