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
  // bars until a route change.  30s balances freshness against API
  // load.  Once we know the project has NO live runs (review-pass
  // J), we stop polling — a batch-only project shouldn't burn an
  // API call every 30s in the background.
  const _REFRESH_MS = 30_000;
  const timeline = useQuery({
    queryKey: ["live-timeline", project],
    queryFn: () => listProjectLiveTimeline(project, { days: 30 }),
    refetchInterval: (q) => {
      const entries = q.state.data?.entries;
      // Initial load: poll until we know the project state.  After
      // first success: poll only when there are entries to refresh.
      if (entries === undefined) return _REFRESH_MS;
      return entries.length > 0 ? _REFRESH_MS : false;
    },
  });
  const agg = useQuery({
    queryKey: ["live-aggregate", project, activeFrom ?? null, activeTo ?? null],
    queryFn: () => getLiveAggregate(project, { from: activeFrom, to: activeTo }),
    refetchInterval: _REFRESH_MS,
    // Pause when the timeline is empty — no calls to aggregate over.
    enabled: (timeline.data?.entries.length ?? 1) > 0,
  });

  if (timeline.isPending) {
    // Round-7 review-pass: render a fixed-height placeholder card
    // instead of ``null`` to prevent the layout shift the rest of
    // the page experiences when the strip pops in 100-200 ms after
    // first paint.  ``h-[140px]`` matches the rendered card
    // height (header line + bar row + day-labels + padding) so
    // the calls list below sits in its final position from frame 1.
    return (
      <Card>
        <div
          data-testid="live-timeline-loading"
          className="h-[140px] animate-pulse text-sm text-[var(--color-fg-muted)]"
        >
          Loading live timeline…
        </div>
      </Card>
    );
  }
  const entries = timeline.data?.entries ?? [];
  if (entries.length === 0) return null; // batch-only project — skip

  // PROXY-2.5 review-pass A: pad to a dense 30-day window so a
  // sparse strip (3 bars with 27 silent days between them) doesn't
  // mislead.  The server returns ONLY days that have rows; the
  // operator needs to see the gaps so "3 recent bars" isn't read as
  // "3 days of continuous traffic".  Synthetic empty entries get
  // ``row_count = 0`` and render as a hairline (review-pass B).
  const denseEntries = padToWindow(entries, 30);
  const maxRows = Math.max(...denseEntries.map((e) => e.row_count), 1);
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
        {denseEntries.map((e) => (
          <TimelineBar
            key={e.run_id || e.started_at || Math.random()}
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
  const isEmpty = entry.row_count === 0;
  // PROXY-2.5 review-pass B: zero-call days render as a 2px hairline
  // (not a 8% green stub) so the gap between active days is
  // unambiguously empty.  Real-traffic days size proportionally
  // with a 12% floor so a single call still produces a visible bar.
  const pct = isEmpty
    ? 0
    : Math.max(12, Math.round((entry.row_count / maxRows) * 100));
  const passRate = entry.row_count > 0
    ? entry.row_pass_count / entry.row_count
    : 1;
  // Day-boundary window: started_at is ``<YYYY-MM-DD>T00:00:00+00:00``
  // (stamped at lazy-create by the proxy for real days, synthesised
  // for empty days via ``padToWindow``).  ``to`` is +1 day.
  const from = entry.started_at ?? "";
  const to   = from ? plusOneDay(from) : "";
  const tooltipText = isEmpty
    ? `${shortDate(from)} · no live traffic`
    : `${shortDate(from)} · ${entry.row_count.toLocaleString()} calls · ${(passRate * 100).toFixed(1)}% pass · $${entry.cost_usd.toFixed(2)}`;

  return (
    <button
      type="button"
      data-testid="live-timeline-day"
      data-run-id={entry.run_id}
      data-empty={isEmpty ? "true" : "false"}
      aria-pressed={active}
      // Empty days aren't clickable — there's nothing to drill into.
      disabled={isEmpty}
      onClick={() => !isEmpty && from && to && onClick({ from, to })}
      title={tooltipText}
      className={
        "group flex w-8 shrink-0 flex-col items-center gap-1 rounded px-0.5 py-1 transition " +
        (isEmpty
          ? "cursor-default opacity-60"
          : active
            ? "bg-[var(--color-bg-row)] ring-1 ring-[var(--color-accent)]"
            : "hover:bg-[var(--color-bg-row)]") +
        " focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]"
      }
    >
      <div
        className={isEmpty ? "w-full" : "w-full rounded"}
        style={{
          height: isEmpty ? 2 : `${pct}%`,
          minHeight: isEmpty ? 2 : 4,
          backgroundColor: isEmpty
            ? "var(--color-border)"
            : passRate >= 0.98
              ? "var(--color-pass)"
              : passRate >= 0.9
                ? "var(--color-warn)"
                : "var(--color-fail)",
        }}
        aria-label={isEmpty
          ? `${shortDate(from)} no traffic`
          : `${entry.row_count} calls`}
      />
      <span className="text-[10px] text-[var(--color-fg-muted)]">
        {shortDate(from)}
      </span>
    </button>
  );
}


/** Pad a sparse timeline to a contiguous ``days``-day window ending
    today (UTC).  The server returns ONLY days that have rows; we
    synthesise zero-count entries for the gaps so the strip's gap
    semantics is honest.  Synthetic entries carry an empty
    ``run_id`` (no real parent run exists for that day yet — the
    proxy lazy-creates only on first call).

    Exported for vitest coverage (round-3 review-pass K).  Tests pass
    a fixed ``today`` so the bucketing is deterministic without
    mocking the clock; production callers omit it. */
export function padToWindow(
  entries: LiveTimelineEntry[],
  days: number,
  today: Date = new Date(),
): LiveTimelineEntry[] {
  const byDate = new Map<string, LiveTimelineEntry>();
  for (const e of entries) {
    if (e.started_at) byDate.set(e.started_at.slice(0, 10), e);
  }
  const out: LiveTimelineEntry[] = [];
  // Clone before mutating so a caller-supplied ``today`` isn't
  // side-effected.  Floor to UTC midnight so the window math is
  // day-aligned regardless of when the page renders.
  const anchor = new Date(today);
  anchor.setUTCHours(0, 0, 0, 0);
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(anchor);
    d.setUTCDate(d.getUTCDate() - i);
    const key = d.toISOString().slice(0, 10);
    const existing = byDate.get(key);
    if (existing) {
      out.push(existing);
    } else {
      out.push({
        run_id: "",
        started_at: `${key}T00:00:00+00:00`,
        finished_at: null,
        row_count: 0,
        row_pass_count: 0,
        row_fail_count: 0,
        cost_usd: 0,
      });
    }
  }
  return out;
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
