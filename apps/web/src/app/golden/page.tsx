"use client";

import { Suspense, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { ConnectionGate } from "@/components/ConnectionGate";
import { GoldenRowPreview } from "@/components/GoldenRowPreview";
import {
  listGoldenCandidates, listProjects, unPromoteGolden,
  type GoldenCandidate,
} from "@/lib/api";
import {
  candidateToJsonlRow, composeJsonl, triggerDownload,
} from "@/lib/golden-export";


/**
 * /golden/?project=customer-service — the golden-DB view.
 *
 * A comfortable curation surface, not just a metadata list:
 *  - project picker (no URL editing)
 *  - inline row preview (input / expected / output) via ?expand=row
 *  - free-text search across row_id / note / reviewer / content
 *  - sortable columns (when / reviewer / row)
 *  - bulk select → remove or download-selected
 *  - in-browser "Download JSONL" (matches `evalguard golden export`)
 *  - per-row "Copy as JSON" to clipboard
 *  - un-promote + the CLI-export hint
 */
export default function GoldenPage() {
  return (
    <ConnectionGate>
      <Suspense fallback={<p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}>
        <Inner />
      </Suspense>
    </ConnectionGate>
  );
}


type SortKey = "when" | "reviewer" | "row";


function Inner() {
  const router  = useRouter();
  const params  = useSearchParams();
  const project = params.get("project") ?? "";

  // Project picker — always shown so you can switch without URL
  // editing.  Populated from the projects the caller can see.
  const projectsQ = useQuery({
    queryKey: ["projects"],
    queryFn: () => listProjects(),
    refetchOnWindowFocus: false,
  });

  function pick(slug: string) {
    router.replace(slug ? `/golden/?project=${encodeURIComponent(slug)}` : "/golden/");
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Golden dataset</h1>
        <label className="text-xs text-[var(--color-fg-muted)]" htmlFor="golden-project">
          project
        </label>
        <select
          id="golden-project"
          data-testid="golden-project-picker"
          value={project}
          onChange={(e) => pick(e.target.value)}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] px-2 py-1 text-sm"
        >
          <option value="">— select —</option>
          {(projectsQ.data?.projects ?? []).map((p) => (
            <option key={p.project_id} value={p.slug}>{p.name}</option>
          ))}
        </select>
        {project && (
          <Link
            href={`/calls/?project=${encodeURIComponent(project)}`}
            className="ml-auto text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
          >
            ← back to calls
          </Link>
        )}
      </div>

      {project ? (
        // ``key`` forces a full unmount/remount when the picker
        // switches projects so ``Body``'s local state (selected ids,
        // filter, sort, expanded row) resets cleanly.  Without it the
        // stale ``selected`` set — holding auto-increment ids from the
        // previous project — would target the WRONG rows on bulk
        // remove.
        <Body key={project} projectSlug={project} />
      ) : (
        <Card>
          <p className="text-sm text-[var(--color-fg-muted)]">
            Pick a project to view its staged golden candidates.
          </p>
        </Card>
      )}
    </div>
  );
}


function Body({ projectSlug }: { projectSlug: string }) {
  const qc = useQueryClient();
  const q  = useQuery({
    queryKey: ["golden-candidates", projectSlug, "expanded"],
    // ``expand: row`` so the preview + download have content without
    // a per-row fetch.
    queryFn: () => listGoldenCandidates(projectSlug, { expand: "row", limit: 500 }),
    refetchOnWindowFocus: false,
  });

  const [filter, setFilter]   = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("when");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [copiedId, setCopiedId]     = useState<number | null>(null);
  const [copyFailedId, setCopyFailedId] = useState<number | null>(null);
  // Per-id in-flight tracking so one row's "remove" doesn't disable
  // every other row's button.  Also drives bulk-remove error
  // retention.
  const [removingIds, setRemovingIds] = useState<Set<number>>(new Set());

  const del = useMutation({
    mutationFn: (id: number) => unPromoteGolden(id),
  });

  function removeOne(id: number) {
    setRemovingIds((p) => new Set(p).add(id));
    del.mutateAsync(id)
      .then(() => {
        setSelected((p) => { const n = new Set(p); n.delete(id); return n; });
      })
      .catch(() => { /* leave it selected so the user can retry */ })
      .finally(() => {
        setRemovingIds((p) => { const n = new Set(p); n.delete(id); return n; });
        qc.invalidateQueries({ queryKey: ["golden-candidates", projectSlug] });
      });
  }

  const candidates = useMemo(
    () => filterAndSort(q.data?.candidates ?? [], filter, sortKey),
    [q.data, filter, sortKey],
  );

  if (q.isPending)
    return <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>;
  if (q.error)
    return (
      <p className="text-sm text-[var(--color-fail)]">
        {q.error instanceof Error ? q.error.message : String(q.error)}
      </p>
    );

  const total = q.data?.candidates.length ?? 0;

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }
  function toggleAll() {
    setSelected((prev) =>
      prev.size === candidates.length
        ? new Set()
        : new Set(candidates.map((c) => c.id)),
    );
  }

  function download(subset: GoldenCandidate[]) {
    const { jsonl, exportedCount } = composeJsonl(subset);
    if (exportedCount === 0) return;
    // Sanitise the slug for the filename even though slugs are
    // server-constrained to ``[a-z0-9-]`` — the value comes off the
    // URL query string, which a user can set to anything before the
    // fetch resolves.
    const safe = projectSlug.replace(/[^a-z0-9-]/gi, "_");
    triggerDownload(`${safe}-golden.jsonl`, jsonl);
  }

  async function copyRow(c: GoldenCandidate) {
    const line = candidateToJsonlRow(c);
    if (line === null) return;
    try {
      // ``navigator.clipboard`` is undefined in an insecure (http)
      // context; guard so we surface a "copy failed" state instead
      // of throwing.
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(line);
      setCopyFailedId((cur) => (cur === c.id ? null : cur));
      setCopiedId(c.id);
      setTimeout(() => setCopiedId((cur) => (cur === c.id ? null : cur)), 1500);
    } catch {
      // Insecure context / permissions blocked — tell the user so
      // they reach for the Download button instead of clicking copy
      // into a void.
      setCopyFailedId(c.id);
      setTimeout(() => setCopyFailedId((cur) => (cur === c.id ? null : cur)), 2500);
    }
  }

  const selectedCandidates = candidates.filter((c) => selected.has(c.id));

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <input
          data-testid="golden-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="filter by row / note / reviewer / content"
          className="flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-card)] px-2 py-1 text-sm"
        />
        <button
          type="button"
          data-testid="golden-download-all"
          onClick={() => download(candidates)}
          disabled={candidates.length === 0}
          className="rounded border border-[var(--color-accent)] px-3 py-1 text-xs hover:bg-[var(--color-bg-row)] disabled:opacity-40"
        >
          Download JSONL ({candidates.length})
        </button>
        {selected.size > 0 && (
          <>
            <button
              type="button"
              data-testid="golden-download-selected"
              onClick={() => download(selectedCandidates)}
              className="rounded border border-[var(--color-accent)] px-3 py-1 text-xs hover:bg-[var(--color-bg-row)]"
            >
              Download selected ({selected.size})
            </button>
            <button
              type="button"
              data-testid="golden-remove-selected"
              onClick={() => {
                // ``removeOne`` removes each id from ``selected`` only
                // on its own success — a failed delete (403 / 500)
                // stays selected so the user can retry just the
                // failures rather than losing the whole selection.
                selectedCandidates.forEach((c) => removeOne(c.id));
              }}
              className="rounded border border-[var(--color-border)] px-3 py-1 text-xs text-[var(--color-fail)] hover:bg-[var(--color-bg-row)]"
            >
              Remove selected ({selected.size})
            </button>
          </>
        )}
      </div>

      <Card>
        {total === 0 ? (
          <p className="text-sm text-[var(--color-fg-muted)]">
            No staged candidates yet.  Promote a call from the{" "}
            <Link
              href={`/calls/?project=${encodeURIComponent(projectSlug)}`}
              className="text-[var(--color-accent)] hover:underline"
            >
              calls stream
            </Link>
            .
          </p>
        ) : candidates.length === 0 ? (
          <p className="text-sm text-[var(--color-fg-muted)]">
            No candidates match <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">{filter}</code>.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
              <tr className="border-b border-[var(--color-border)]">
                <th className="px-2 py-1.5 text-left font-medium">
                  <input
                    type="checkbox"
                    data-testid="golden-select-all"
                    checked={selected.size === candidates.length && candidates.length > 0}
                    onChange={toggleAll}
                    aria-label="select all"
                  />
                </th>
                <SortableTh label="Row"      active={sortKey === "row"}      onClick={() => setSortKey("row")} />
                <th className="px-2 py-1.5 text-left font-medium">Run</th>
                <SortableTh label="Reviewer" active={sortKey === "reviewer"} onClick={() => setSortKey("reviewer")} />
                <th className="px-2 py-1.5 text-left font-medium">Note</th>
                <SortableTh label="When"     active={sortKey === "when"}     onClick={() => setSortKey("when")} />
                <th className="px-2 py-1.5 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c) => (
                <RowGroup
                  key={c.id}
                  c={c}
                  projectSlug={projectSlug}
                  selected={selected.has(c.id)}
                  expanded={expandedId === c.id}
                  copied={copiedId === c.id}
                  copyFailed={copyFailedId === c.id}
                  removing={removingIds.has(c.id)}
                  onToggleSelect={() => toggle(c.id)}
                  onToggleExpand={() => setExpandedId((cur) => (cur === c.id ? null : c.id))}
                  onCopy={() => copyRow(c)}
                  onRemove={() => removeOne(c.id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {total > 0 && (
        <Card title="Export from the CLI">
          <p className="text-xs text-[var(--color-fg-muted)]">
            The Download button above produces the same JSONL as the CLI.
            For CI / scripted exports:
          </p>
          <pre
            data-testid="golden-export-hint"
            className="mt-2 overflow-x-auto rounded bg-[var(--color-bg-row)] px-3 py-2 font-mono text-xs"
          >
            evalguard golden export --project {projectSlug} --to datasets/golden.jsonl
          </pre>
        </Card>
      )}
    </div>
  );
}


function RowGroup({
  c, projectSlug, selected, expanded, copied, copyFailed, removing,
  onToggleSelect, onToggleExpand, onCopy, onRemove,
}: {
  c: GoldenCandidate;
  projectSlug: string;
  selected: boolean;
  expanded: boolean;
  copied: boolean;
  copyFailed: boolean;
  removing: boolean;
  onToggleSelect: () => void;
  onToggleExpand: () => void;
  onCopy: () => void;
  onRemove: () => void;
}) {
  const canCopy = candidateToJsonlRow(c) !== null;
  return (
    <>
      <tr
        data-testid="golden-row"
        data-candidate-id={c.id}
        data-row-id={c.row_id}
        className="border-b border-[var(--color-border)]"
      >
        <td className="px-2 py-1.5">
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggleSelect}
            aria-label={`select ${c.row_id}`}
          />
        </td>
        <td className="px-2 py-1.5 font-mono text-xs">
          <button
            type="button"
            data-testid="golden-expand"
            onClick={onToggleExpand}
            className="text-[var(--color-accent)] hover:underline"
          >
            {expanded ? "▾ " : "▸ "}{c.row_id}
          </button>
        </td>
        <td className="px-2 py-1.5 font-mono text-xs">
          <Link
            href={
              `/calls/?project=${encodeURIComponent(projectSlug)}`
              + `&call=${encodeURIComponent(`${c.run_id}:${c.row_id}`)}`
            }
            className="text-[var(--color-accent)] hover:underline"
          >
            {c.run_id}
          </Link>
        </td>
        <td className="px-2 py-1.5 font-mono text-xs">{c.promoted_by}</td>
        <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
          {c.note ?? "—"}
        </td>
        <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]" title={c.created_at}>
          {fmtTime(c.created_at)}
        </td>
        <td className="px-2 py-1.5 text-right">
          <div className="flex justify-end gap-2">
            <button
              type="button"
              data-testid="golden-copy"
              onClick={onCopy}
              disabled={!canCopy}
              title={
                !canCopy ? "No exportable content"
                : copyFailed ? "Clipboard blocked (try the Download button)"
                : "Copy this row as JSON"
              }
              className={
                "rounded border px-2 py-0.5 text-xs hover:bg-[var(--color-bg-row)] disabled:opacity-40 " +
                (copyFailed
                  ? "border-[var(--color-fail)] text-[var(--color-fail)]"
                  : "border-[var(--color-border)]")
              }
            >
              {copyFailed ? "copy failed" : copied ? "✓ copied" : "copy"}
            </button>
            <button
              type="button"
              data-testid="golden-unpromote"
              disabled={removing}
              onClick={onRemove}
              className="rounded border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)] disabled:opacity-40"
            >
              remove
            </button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr data-testid="golden-row-expanded" className="border-b border-[var(--color-border)]">
          <td />
          <td colSpan={6} className="px-2 py-2">
            <GoldenRowPreview rowData={c.row_data} />
          </td>
        </tr>
      )}
    </>
  );
}


function SortableTh({
  label, active, onClick,
}: { label: string; active: boolean; onClick: () => void }) {
  return (
    // ``aria-sort`` on the <th> is the correct ARIA pattern for a
    // sortable column header (not ``aria-pressed`` on the button —
    // that's the toggle-button pattern).  We only sort descending
    // today, so the active column is always "descending"; others
    // are "none".
    <th
      className="px-2 py-1.5 text-left font-medium"
      aria-sort={active ? "descending" : "none"}
    >
      <button
        type="button"
        onClick={onClick}
        className={
          "hover:text-[var(--color-fg)] " +
          (active ? "text-[var(--color-fg)]" : "")
        }
      >
        {label}{active ? " ↓" : ""}
      </button>
    </th>
  );
}


/** Pure: filter by free text across the visible + content fields,
 *  then sort.  Extracted-shape so it's covered by the page's vitest
 *  without rendering. */
export function filterAndSort(
  candidates: GoldenCandidate[],
  filter: string,
  sortKey: SortKey,
): GoldenCandidate[] {
  const needle = filter.trim().toLowerCase();
  const matched = needle
    ? candidates.filter((c) => {
        const hay = [
          c.row_id, c.run_id, c.promoted_by, c.note ?? "",
          // Include the row content so "find the candidate about
          // refunds" works even when the row_id is opaque.  Match
          // the preview's rendering — ``stringifyForSearch`` covers
          // structured (object / array) inputs too, not just plain
          // strings, so a RAG row with ``input: {query: "refunds"}``
          // is findable.
          stringifyForSearch(c.row_data?.input),
          stringifyForSearch(c.row_data?.expected),
          stringifyForSearch(c.row_data?.output),
        ].join(" ").toLowerCase();
        return hay.includes(needle);
      })
    : candidates.slice();

  // Stable, locale-independent sort.  ISO-8601 timestamps + ids are
  // pure ASCII, so a plain lexical compare orders them correctly
  // without ``localeCompare``'s browser-locale variance.  Every
  // branch falls back to an id tiebreaker so equal primary keys
  // keep a deterministic order (mirrors the server's
  // ``ORDER BY ..., id DESC``).
  matched.sort((a, b) => {
    switch (sortKey) {
      case "reviewer":
        return cmp(a.promoted_by, b.promoted_by) || (a.id - b.id);
      case "row":
        return cmp(a.row_id, b.row_id) || (a.id - b.id);
      case "when":
      default:
        // Newest-first: descending created_at, then descending id.
        return cmp(b.created_at, a.created_at) || (b.id - a.id);
    }
  });
  return matched;
}


/** Lexical (ASCII) string compare — locale-independent, unlike
 *  ``String.prototype.localeCompare``. */
function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}


/** Stringify any value for the search haystack — strings pass
 *  through, structured values JSON-stringify (matching what
 *  ``GoldenRowPreview`` renders), null/undefined → "". */
function stringifyForSearch(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
}


function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
