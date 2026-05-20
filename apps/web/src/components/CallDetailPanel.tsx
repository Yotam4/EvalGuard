/**
 * Drill-down panel for one call.  Renders the ``CallDetail`` API
 * response — input / expected / output side-by-side, scores
 * (with thresholds where the evaluator emitted them), and the
 * parent trial's gate verdicts as context.
 *
 * Pure presentational — fetching is the parent's job.  Side-by-side
 * is realised as a 1-col stack on mobile and 2-col on ≥md screens.
 */

"use client";

import { Badge } from "./Badge";
import type { CallDetail, Score } from "@/lib/api";


export function CallDetailPanel({
  data,
  onClose,
}: {
  data: CallDetail;
  onClose?: () => void;
}) {
  return (
    <div
      data-testid="call-detail-panel"
      data-run-id={data.run_id}
      data-row-id={data.row_id}
      className="space-y-4 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] p-4"
    >
      <Header data={data} onClose={onClose} />
      <ContentGrid data={data} />
      {data.scores.length > 0 && <ScoresTable scores={data.scores} />}
      {data.trial_gates.length > 0 && <TrialGates gates={data.trial_gates} />}
    </div>
  );
}


function Header({
  data,
  onClose,
}: {
  data: CallDetail;
  onClose?: () => void;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-3">
      <h2 className="font-mono text-sm font-semibold">{data.row_id}</h2>
      <Badge tone={data.passed ? "pass" : "fail"}>
        {data.passed ? "PASS" : "FAIL"}
      </Badge>
      {data.provider && data.model && (
        <span className="text-xs text-[var(--color-fg-muted)]">
          {data.provider}:{data.model}
        </span>
      )}
      <span className="text-xs text-[var(--color-fg-muted)]">
        run <span className="font-mono">{data.run_id}</span>
      </span>
      <span className="ml-auto flex items-center gap-3 text-xs text-[var(--color-fg-muted)]">
        <span className="font-mono">{data.latency_ms} ms</span>
        <span className="font-mono">${data.cost_usd.toFixed(4)}</span>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            data-testid="call-detail-close"
            className="rounded border border-[var(--color-border)] px-2 py-0.5 hover:bg-[var(--color-bg-row)]"
          >
            close
          </button>
        )}
      </span>
    </div>
  );
}


function ContentGrid({ data }: { data: CallDetail }) {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      <ContentBlock label="Input"    value={data.input} />
      <ContentBlock label="Expected" value={data.expected} />
      <ContentBlock label="Output"   value={data.output} colSpan={2} />
    </div>
  );
}


function ContentBlock({
  label, value, colSpan = 1,
}: {
  label: string;
  value: unknown;
  colSpan?: 1 | 2;
}) {
  const text = renderValue(value);
  return (
    <div
      data-testid={`call-content-${label.toLowerCase()}`}
      className={
        "rounded border border-[var(--color-border)] bg-[var(--color-bg)] p-3 " +
        (colSpan === 2 ? "md:col-span-2" : "")
      }
    >
      <div className="mb-1 text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        {label}
      </div>
      {text === null ? (
        <p className="text-xs italic text-[var(--color-fg-muted)]">—</p>
      ) : (
        <pre className="whitespace-pre-wrap break-words font-mono text-xs">
          {text}
        </pre>
      )}
    </div>
  );
}


function renderValue(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v;
  // ``input`` / ``expected`` can be arbitrary JSON (dict, list,
  // number…).  Pretty-print so a structured input survives the
  // detail view with its shape intact.
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}


function ScoresTable({ scores }: { scores: Score[] }) {
  return (
    <div>
      <h3 className="mb-2 text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        Scores
      </h3>
      <table className="w-full text-sm">
        <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
          <tr className="border-b border-[var(--color-border)]">
            <th className="px-2 py-1.5 text-left font-medium">Evaluator</th>
            <th className="px-2 py-1.5 text-left font-medium">Kind</th>
            <th className="px-2 py-1.5 text-right font-medium">Layer</th>
            <th className="px-2 py-1.5 text-right font-medium">Value</th>
            <th className="px-2 py-1.5 text-left font-medium">Result</th>
          </tr>
        </thead>
        <tbody>
          {scores.map((s, i) => (
            <tr
              key={`${s.evaluator_id}-${i}`}
              data-testid="call-score-row"
              data-evaluator-id={s.evaluator_id}
              className="border-b border-[var(--color-border)]"
            >
              <td className="px-2 py-1.5 font-mono text-xs">{s.evaluator_id}</td>
              <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
                {s.evaluator_kind}
              </td>
              <td className="px-2 py-1.5 text-right text-xs">{s.layer}</td>
              <td className="px-2 py-1.5 text-right font-mono text-xs">
                {fmtScore(s.value)}
              </td>
              <td className="px-2 py-1.5">
                <Badge tone={s.passed ? "pass" : "fail"}>
                  {s.passed ? "pass" : "fail"}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function fmtScore(v: number): string {
  if (!isFinite(v)) return "—";
  if (Math.abs(v) >= 100) return v.toFixed(1);
  if (Math.abs(v) >= 1)   return v.toFixed(3);
  return v.toFixed(4);
}


function TrialGates({ gates }: { gates: CallDetail["trial_gates"] }) {
  return (
    <div>
      <h3 className="mb-2 text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        Trial gates (context)
      </h3>
      <table className="w-full text-sm">
        <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
          <tr className="border-b border-[var(--color-border)]">
            <th className="px-2 py-1.5 text-left font-medium">Gate</th>
            <th className="px-2 py-1.5 text-left font-medium">Severity</th>
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
                    g.severity === "block" ? "fail"
                      : g.severity === "warn" ? "warn"
                      : "muted"
                  }
                >
                  {g.severity}
                </Badge>
              </td>
              <td className="px-2 py-1.5">
                <Badge tone={g.passed ? "pass" : "fail"}>
                  {g.passed ? "pass" : "fail"}
                </Badge>
              </td>
              <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
                {g.details.length > 0 ? `${g.details.length} rule(s)` : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
