"use client";

import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import {
  getLiveAggregate, listProjectLiveTimeline,
  type LiveTimelineEntry,
} from "@/lib/api";

/**
 * /calls/ live-run timeline strip — Phase PROXY-2.5.
 *
 * Renders one horizontal bar per day of proxied traffic, sized by
 * call count and tinted by pass-rate.  Clicking a bar sets the
 * ``[from, to)`` window on ``/calls/`` so the stream below narrows
 * to that day's calls.  Clicking the active bar again clears the
 * window.
 *
 * Hidden when the project has no live runs at all — keeps batch-only
 * projects (CLI / OTLP) from carrying unused real estate.
 */
export function LiveTimeline({
  project, activeFrom, activeTo, onPick,
}: {
  project: string;
  activeFrom: string | undefined;
  activeTo:   string | undefined;
  onPick: (window: { from: string; to: string } | null) => void;
}) {
  // Live traffic mutates the underlying data continuously, so the
  // timeline + aggregate are auto-refreshed every 30s.  Without this
  // an operator watching incoming production calls would see stale
  // bars until a route change — exactly the wrong UX for the
  // "verbose inspection" surface this view is built for.  30s
  // balances freshness against API load: the timeline endpoint is
  // a cheap aggregate, but a 1s refetch on a busy dashboard would
  // still add real cost.
  const _REFRESH_MS = 30_000;
  const timeline = useQuery({
    queryKey: ["live-timeline", project],
    queryFn: () => listProjectLiveTimeline(project, { days: 30 }),
    refetchInterval: _REFRESH_MS,
  });
  const agg = useQuery({
    queryKey: ["live-aggregate", project, activeFrom ?? null, activeTo ?? null],
    queryFn: () => getLiveAggregate(project, { from: activeFrom, to: activeTo }),
    refetchInterval: _REFRESH_MS,
  });

  if (timeline.isPending) return null;   // silent on first paint
  const entries = timeline.data?.entries ?? [];
  if (entries.length === 0) return null; // batch-only project — skip

  const maxRows = Math.max(...entries.map((e) => e.row_count), 1);
  const aggData = agg.data;

  return (
    <Card>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">Live timeline</h2>
        {aggData && (
          <span
            data-testid="live-aggregate-banner"
            className="text-xs text-[var(--color-fg-muted)]"
          >
            {aggData.row_count.toLocaleString()} calls
            {aggData.run_count > 1 && (
              <> across {aggData.run_count} runs</>
            )}
            {aggData.row_count > 0 && (
              <> · {((aggData.row_pass_count / aggData.row_count) * 100).toFixed(1)}% pass</>
            )}
            {aggData.cost_usd > 0 && (
              <> · ${aggData.cost_usd.toFixed(2)}</>
            )}
          </span>
        )}
      </div>
      <div
        data-testid="live-timeline-bars"
        className="mt-3 flex items-end gap-1 overflow-x-auto"
      >
        {entries.slice().reverse().map((e) => (
          <TimelineBar
            key={e.run_id}
            entry={e}
            maxRows={maxRows}
            active={isActive(e, activeFrom, activeTo)}
            onClick={(win) => {
              // Re-click on the active bar clears the window.
              if (isActive(e, activeFrom, activeTo)) onPick(null);
              else onPick(win);
            }}
          />
        ))}
      </div>
    </Card>
  );
}


function TimelineBar({
  entry, maxRows, active, onClick,
}: {
  entry: LiveTimelineEntry;
  maxRows: number;
  active: boolean;
  onClick: (window: { from: string; to: string }) => void;
}) {
  const pct = Math.max(8, Math.round((entry.row_count / maxRows) * 100));
  const passRate = entry.row_count > 0
    ? entry.row_pass_count / entry.row_count
    : 1;
  // Day-boundary window: started_at is ``<YYYY-MM-DD>T00:00:00+00:00``
  // (stamped at lazy-create by the proxy) and ``to`` is +1 day.
  const from = entry.started_at ?? "";
  const to   = from ? plusOneDay(from) : "";

  return (
    <button
      type="button"
      data-testid="live-timeline-day"
      data-run-id={entry.run_id}
      aria-pressed={active}
      onClick={() => from && to && onClick({ from, to })}
      title={`${shortDate(from)} · ${entry.row_count.toLocaleString()} calls · ${(passRate * 100).toFixed(1)}% pass · $${entry.cost_usd.toFixed(2)}`}
      className={
        "group flex w-8 shrink-0 flex-col items-center gap-1 rounded px-0.5 py-1 transition " +
        (active
          ? "bg-[var(--color-bg-row)] ring-1 ring-[var(--color-accent)]"
          : "hover:bg-[var(--color-bg-row)]")
      }
    >
      <div
        className="w-full rounded"
        style={{
          height: `${pct}%`,
          minHeight: 4,
          backgroundColor: passRate >= 0.98
            ? "var(--color-pass)"
            : passRate >= 0.9
              ? "var(--color-warn, #d4a017)"
              : "var(--color-fail)",
        }}
        aria-label={`${entry.row_count} calls`}
      />
      <span className="text-[10px] text-[var(--color-fg-muted)]">
        {shortDate(from)}
      </span>
    </button>
  );
}


/** Pull the day-of-month off the ISO date so the timeline labels
    stay 2 chars wide.  ``"2026-05-29T00:00:00+00:00"`` → ``"29"``. */
function shortDate(iso: string): string {
  if (!iso || iso.length < 10) return "—";
  return iso.slice(8, 10);
}


/** ``"2026-05-29T00:00:00+00:00"`` → ``"2026-05-30T00:00:00+00:00"``. */
function plusOneDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  d.setUTCDate(d.getUTCDate() + 1);
  // Match the server's canonical form so URL params round-trip cleanly.
  return d.toISOString().replace(/\.\d+Z$/, "+00:00").replace(/Z$/, "+00:00");
}


function isActive(
  entry: LiveTimelineEntry,
  activeFrom: string | undefined,
  activeTo:   string | undefined,
): boolean {
  // Compare by epoch milliseconds rather than raw strings.  The
  // server may stamp ``started_at`` as ``2026-05-29T00:00:00+00:00``
  // but a future schema bump or a hand-crafted URL could send
  // ``2026-05-29T00:00:00.000+00:00`` or ``...Z``; ``===`` on the
  // raw strings would silently miss the highlight.  ``new Date(x)
  // .getTime()`` normalises every reasonable ISO-8601 form.
  if (!activeFrom || !activeTo || !entry.started_at) return false;
  const entryFrom = new Date(entry.started_at).getTime();
  const entryTo   = new Date(plusOneDay(entry.started_at)).getTime();
  const wantFrom  = new Date(activeFrom).getTime();
  const wantTo    = new Date(activeTo).getTime();
  if ([entryFrom, entryTo, wantFrom, wantTo].some(Number.isNaN)) return false;
  return entryFrom === wantFrom && entryTo === wantTo;
}
