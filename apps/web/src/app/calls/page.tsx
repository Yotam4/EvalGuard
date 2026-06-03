"use client";

import { Suspense, useRef } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";

import { Card } from "@/components/Card";
import { CallCard } from "@/components/CallCard";
import { CallDetailPanel } from "@/components/CallDetailPanel";
import { ConnectionGate } from "@/components/ConnectionGate";
import { LiveTimeline } from "@/components/LiveTimeline";
import {
  fmtError, getCallDetail, listProjectCalls,
  type CallSummary, type CallsTab, type RunSource,
} from "@/lib/api";


/**
 * /calls/ — per-call observability stream.
 *
 * URL: ``/calls/?project=customer-service&tab=recent|failures|passed&source=&from=&to=&call=run_x:r-1``
 *
 * - ``project`` (required) — which project's calls to stream
 * - ``tab``      — recent (default), failures, or passed (PROXY-2.5)
 * - ``source``   — optional cli / otlp / live narrowing
 * - ``from`` / ``to`` (PROXY-2.5) — half-open ``[from, to)`` window
 *   on ``ingested_at``. Set automatically when the operator clicks a
 *   day in the live-timeline strip.
 * - ``call``     — selected call as ``run_id:row_id``, opens the
 *                  side panel.  All on the URL so back-button works
 *                  and the link is shareable.
 *
 * The list is virtualized via ``@tanstack/react-virtual`` so 10k
 * cards render at ~constant memory.  Cursor-paginated via
 * ``useInfiniteQuery`` — each page fetches when the viewport
 * approaches the bottom of the rendered window.
 */
export default function CallsPage() {
  return (
    <ConnectionGate>
      <Suspense fallback={<p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}>
        <Inner />
      </Suspense>
    </ConnectionGate>
  );
}


function Inner() {
  const router  = useRouter();
  const params  = useSearchParams();
  const project = params.get("project") ?? "";
  const tabParam = params.get("tab");
  const tab: CallsTab =
    tabParam === "failures" || tabParam === "passed" ? tabParam : "recent";
  const sourceParam = params.get("source");
  const source: RunSource | undefined =
    sourceParam === "cli" || sourceParam === "otlp" || sourceParam === "live"
      ? sourceParam
      : undefined;
  // PROXY-2.5: optional ``from`` / ``to`` window on ``ingested_at``.
  // The server treats them as a half-open ``[from, to)`` interval,
  // which is what the timeline-day-click + drag-select land on.
  const from = params.get("from") ?? undefined;
  const to   = params.get("to")   ?? undefined;
  const selected = params.get("call");  // "run_id:row_id" or null
  const selectedTrial = params.get("trial");  // trial_id or null

  function setParam(name: string, value: string | null) {
    setParams({ [name]: value });
  }
  // Batch setter so selecting a call can update ``call`` + ``trial``
  // in one ``router.replace`` (two sequential ``setParam`` calls
  // would race on the stale ``params`` snapshot, dropping one).
  function setParams(updates: Record<string, string | null>) {
    const qs = new URLSearchParams(params);
    for (const [name, value] of Object.entries(updates)) {
      if (value === null || value === "") qs.delete(name);
      else qs.set(name, value);
    }
    router.replace(`/calls/?${qs.toString()}`);
  }

  if (!project) {
    return <ProjectPicker />;
  }

  return (
    <div className="space-y-4">
      <Header
        project={project}
        tab={tab}
        source={source}
        from={from}
        to={to}
        onTab={(t)   => setParam("tab",    t === "recent" ? null : t)}
        onSource={(s) => setParam("source", s ?? null)}
        onClearWindow={() => setParams({ from: null, to: null })}
      />
      {/* PROXY-2.5: timeline strip — clicking a daily bar sets the
          ``[from, to)`` window so the list below narrows to that
          day's calls.  Hidden when the project has no live runs to
          avoid taking up space for batch-only projects. */}
      <LiveTimeline
        project={project}
        activeFrom={from}
        activeTo={to}
        onPick={(window) => setParams({
          from:   window?.from ?? null,
          to:     window?.to   ?? null,
        })}
      />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_2fr]">
        <CallsList
          project={project}
          tab={tab}
          source={source}
          from={from}
          to={to}
          selected={selected}
          onSelect={(call) => setParams({
            call:  `${call.run_id}:${call.row_id}`,
            // Carry the trial so the detail panel disambiguates a
            // row_id shared across trials in a multi-trial run.
            trial: call.trial_id ?? null,
          })}
        />
        <DetailSlot
          project={project}
          selected={selected}
          selectedTrial={selectedTrial}
          onClose={() => setParams({ call: null, trial: null })}
        />
      </div>
    </div>
  );
}


function ProjectPicker() {
  return (
    <Card title="Calls">
      <p className="text-sm text-[var(--color-fg-muted)]">
        The calls stream is per-project.  Pass{" "}
        <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">
          ?project=&lt;slug&gt;
        </code>{" "}
        on the URL to open it.
      </p>
      <Link
        href="/runs/"
        className="mt-3 inline-block rounded border border-[var(--color-border)] px-3 py-1.5 text-sm hover:bg-[var(--color-bg-row)]"
      >
        ← Back to runs
      </Link>
    </Card>
  );
}


function Header({
  project, tab, source, from, to, onTab, onSource, onClearWindow,
}: {
  project: string;
  tab: CallsTab;
  source: RunSource | undefined;
  from: string | undefined;
  to:   string | undefined;
  onTab: (t: CallsTab) => void;
  onSource: (s: RunSource | null) => void;
  onClearWindow: () => void;
}) {
  const windowActive = from || to;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Calls</h1>
        <span className="text-sm text-[var(--color-fg-muted)]">
          project <span className="text-[var(--color-fg)]">{project}</span>
        </span>
        {windowActive && (
          <span
            data-testid="window-chip"
            className="inline-flex items-center gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-row)] px-2 py-0.5 text-xs"
          >
            {formatWindow(from, to)}
            <button
              type="button"
              onClick={onClearWindow}
              className="text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
              aria-label="Clear time window"
            >
              ×
            </button>
          </span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Tabs<CallsTab>
          name="tab"
          value={tab}
          onChange={onTab}
          options={[
            { value: "recent",   label: "Recent" },
            { value: "passed",   label: "Passed" },
            { value: "failures", label: "Failures" },
          ]}
        />
        <Tabs<RunSource | "all">
          name="source"
          value={source ?? "all"}
          onChange={(v) => onSource(v === "all" ? null : (v as RunSource))}
          options={[
            { value: "all",  label: "All sources" },
            { value: "cli",  label: "CLI" },
            { value: "otlp", label: "OTLP" },
            { value: "live", label: "Live (proxy)" },
          ]}
        />
      </div>
    </div>
  );
}


/** Render the active ``[from, to)`` window as a human chip label.
    Shows just the date when both bounds align to UTC midnight (the
    common "click a day" case); otherwise shows both ISO timestamps. */
function formatWindow(from: string | undefined, to: string | undefined): string {
  if (from && to && isUtcMidnight(from) && isUtcMidnight(to)) {
    const d1 = from.slice(0, 10);
    const d2 = isOneDayAfter(from, to) ? null : to.slice(0, 10);
    return d2 ? `${d1} → ${d2}` : d1;
  }
  return [from, to].filter(Boolean).join(" → ") || "—";
}
function isUtcMidnight(iso: string): boolean {
  return /T00:00:00(\.0+)?(\+00:00|Z)?$/.test(iso);
}
function isOneDayAfter(a: string, b: string): boolean {
  // Cheap test: same prefix except the day digit incremented by 1.
  const da = new Date(a).getTime();
  const db = new Date(b).getTime();
  return db - da === 86400000;
}


function Tabs<V extends string>({
  name, value, onChange, options,
}: {
  name: string;
  value: V;
  onChange: (v: V) => void;
  options: { value: V; label: string }[];
}) {
  // ARIA: this is a single-selection filter group, NOT a tablist
  // (the active selection doesn't reveal an adjacent panel — it
  // narrows the same list).  ``role="radiogroup"`` + ``role="radio"``
  // is the correct semantic: AT announces "Recent, radio button,
  // selected" / "Failures, radio button, not selected" with
  // ``aria-checked`` driving the state.  Replaces the earlier
  // ``aria-pressed`` toggle-button pattern that confused screen
  // readers (PROXY-2.5 review-pass).
  return (
    <div
      data-testid={`${name}-tabs`}
      role="radiogroup"
      aria-label={name}
      className="flex flex-wrap gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] p-1"
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            data-tab={o.value}
            aria-checked={active}
            onClick={() => onChange(o.value)}
            className={
              "rounded px-3 py-1 text-xs transition " +
              "focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)] " +
              (active
                ? "bg-[var(--color-bg-row)] text-[var(--color-fg)]"
                : "text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)] hover:text-[var(--color-fg)]")
            }
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}


function CallsList({
  project, tab, source, from, to, selected, onSelect,
}: {
  project: string;
  tab: CallsTab;
  source: RunSource | undefined;
  from: string | undefined;
  to:   string | undefined;
  selected: string | null;
  onSelect: (call: CallSummary) => void;
}) {
  const q = useInfiniteQuery({
    // Include from/to in the key so changing the timeline window
    // refetches from page 1 rather than concatenating pages from
    // different windows.
    queryKey: ["calls", project, tab, source ?? null, from ?? null, to ?? null],
    queryFn: ({ pageParam }) => listProjectCalls(project, {
      tab,
      cursor: pageParam as string | undefined,
      source,
      from,
      to,
      limit: 50,
    }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const calls: CallSummary[] = q.data?.pages.flatMap((p) => p.calls) ?? [];

  const parentRef = useRef<HTMLDivElement | null>(null);
  const rowVirtualizer = useVirtualizer({
    count: calls.length,
    getScrollElement: () => parentRef.current,
    // Conservative size estimate: card with one preview line + meta.
    // Real cards range from ~76px to ~112px; the virtualizer
    // measures actual height on first render and corrects.
    estimateSize: () => 96,
    overscan: 8,
  });

  if (q.isPending) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fg-muted)]">Loading calls…</p>
      </Card>
    );
  }
  if (q.error) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fail)]">
          {fmtError(q.error)}
        </p>
      </Card>
    );
  }
  if (calls.length === 0) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fg-muted)]">
          No calls yet.  Push a run with{" "}
          <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">
            evalguard push --last --scores
          </code>{" "}
          or wire your service to{" "}
          <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">
            POST /v1/otlp/v1/traces
          </code>.
        </p>
      </Card>
    );
  }

  return (
    <Card>
      <div
        ref={parentRef}
        data-testid="calls-list"
        className="max-h-[70vh] overflow-y-auto"
      >
        <div
          style={{
            height: rowVirtualizer.getTotalSize(),
            position: "relative",
            width: "100%",
          }}
        >
          {rowVirtualizer.getVirtualItems().map((vRow) => {
            const call = calls[vRow.index];
            const key = `${call.run_id}:${call.row_id}`;
            return (
              <div
                key={key}
                ref={rowVirtualizer.measureElement}
                data-index={vRow.index}
                style={{
                  position: "absolute",
                  top: 0, left: 0, width: "100%",
                  transform: `translateY(${vRow.start}px)`,
                  padding: "4px 0",
                }}
              >
                <CallCard
                  call={call}
                  selected={selected === key}
                  onSelect={onSelect}
                />
              </div>
            );
          })}
        </div>
        {q.hasNextPage && (
          <div className="mt-3 flex justify-center">
            <button
              type="button"
              onClick={() => q.fetchNextPage()}
              disabled={q.isFetchingNextPage}
              className="rounded border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-bg-row)] disabled:opacity-40"
            >
              {q.isFetchingNextPage ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
      </div>
    </Card>
  );
}


function DetailSlot({
  project, selected, selectedTrial, onClose,
}: {
  project: string;
  selected: string | null;
  selectedTrial: string | null;
  onClose: () => void;
}) {
  if (!selected) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fg-muted)]">
          Select a call on the left to see input / output / scores.
        </p>
      </Card>
    );
  }
  // Selection is encoded as ``run_id:row_id`` on the URL.  ``split(":", 1)``
  // semantics here: take everything BEFORE the first ``:`` as the
  // run_id (run_ids match ``^run_[a-z0-9]{8,}$`` so they have no
  // colons), and everything AFTER as the row_id.  A free-form
  // row_id containing ``:`` (rare but legal up to max_length=200)
  // still round-trips correctly.
  const colon = selected.indexOf(":");
  if (colon === -1) {
    // Malformed selection string — render the empty state rather
    // than fetch garbage.
    return (
      <Card>
        <p className="text-sm text-[var(--color-fail)]">
          Malformed call selection on the URL.
        </p>
      </Card>
    );
  }
  const runId = selected.slice(0, colon);
  const rowId = selected.slice(colon + 1);
  return (
    <DetailFetcher
      project={project} runId={runId} rowId={rowId}
      trialId={selectedTrial} onClose={onClose}
    />
  );
}


function DetailFetcher({
  project, runId, rowId, trialId, onClose,
}: {
  project: string;
  runId: string;
  rowId: string;
  trialId: string | null;
  onClose: () => void;
}) {
  const q = useQuery({
    // trialId in the key so switching between two trials' cards for
    // the same row refetches the right content.
    queryKey: ["call-detail", project, runId, rowId, trialId],
    queryFn: () => getCallDetail(project, runId, rowId, { trialId }),
    refetchOnWindowFocus: false,
  });
  if (q.isPending) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fg-muted)]">Loading call…</p>
      </Card>
    );
  }
  if (q.error) {
    return (
      <Card>
        <div className="flex items-baseline justify-between gap-2">
          <p className="text-sm text-[var(--color-fail)]">
            {fmtError(q.error)}
          </p>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-[var(--color-border)] px-2 py-0.5 text-xs hover:bg-[var(--color-bg-row)]"
          >
            close
          </button>
        </div>
      </Card>
    );
  }
  if (!q.data) return null;
  return (
    <CallDetailPanel
      data={q.data}
      onClose={onClose}
      // Pass the slug so the panel renders the Promote button +
      // invalidates the golden list on success.
      projectSlug={project}
    />
  );
}
