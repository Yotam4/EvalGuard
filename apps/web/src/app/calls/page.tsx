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
import {
  getCallDetail, listProjectCalls,
  type CallSummary, type CallsTab, type RunSource,
} from "@/lib/api";


/**
 * /calls/ — per-call observability stream.
 *
 * URL: ``/calls/?project=customer-service&tab=recent|failures&source=&call=run_x:r-1``
 *
 * - ``project`` (required) — which project's calls to stream
 * - ``tab``      — recent (default) or failures
 * - ``source``   — optional cli / otlp narrowing
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
  const tab: CallsTab =
    params.get("tab") === "failures" ? "failures" : "recent";
  const sourceParam = params.get("source");
  const source: RunSource | undefined =
    sourceParam === "cli" || sourceParam === "otlp" ? sourceParam : undefined;
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
        onTab={(t)   => setParam("tab",    t === "recent" ? null : t)}
        onSource={(s) => setParam("source", s ?? null)}
      />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_2fr]">
        <CallsList
          project={project}
          tab={tab}
          source={source}
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
  project, tab, source, onTab, onSource,
}: {
  project: string;
  tab: CallsTab;
  source: RunSource | undefined;
  onTab: (t: CallsTab) => void;
  onSource: (s: RunSource | null) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Calls</h1>
        <span className="text-sm text-[var(--color-fg-muted)]">
          project <span className="text-[var(--color-fg)]">{project}</span>
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Tabs<CallsTab>
          name="tab"
          value={tab}
          onChange={onTab}
          options={[
            { value: "recent",   label: "Recent" },
            { value: "failures", label: "Failures only" },
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
          ]}
        />
      </div>
    </div>
  );
}


function Tabs<V extends string>({
  name, value, onChange, options,
}: {
  name: string;
  value: V;
  onChange: (v: V) => void;
  options: { value: V; label: string }[];
}) {
  return (
    <div
      data-testid={`${name}-tabs`}
      className="flex flex-wrap gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] p-1"
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            data-tab={o.value}
            aria-pressed={active}
            onClick={() => onChange(o.value)}
            className={
              "rounded px-3 py-1 text-xs transition " +
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
  project, tab, source, selected, onSelect,
}: {
  project: string;
  tab: CallsTab;
  source: RunSource | undefined;
  selected: string | null;
  onSelect: (call: CallSummary) => void;
}) {
  const q = useInfiniteQuery({
    queryKey: ["calls", project, tab, source ?? null],
    queryFn: ({ pageParam }) => listProjectCalls(project, {
      tab,
      cursor: pageParam as string | undefined,
      source,
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
          {q.error instanceof Error ? q.error.message : String(q.error)}
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
            {q.error instanceof Error ? q.error.message : String(q.error)}
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
