/**
 * Per-asset versions table.  Lives in its own file (rather than
 * inlined in ``/assets/detail/page.tsx``) so the row-rendering
 * logic — version-id truncation, ``source`` badge, run link — is
 * unit-testable without react-query / next-navigation scaffolding.
 *
 * Pure presentational: no hooks, no data-fetching.  The parent
 * page owns the query and passes ``versions`` in.
 */

"use client";

import Link from "next/link";

import { Badge } from "./Badge";
import type { AssetVersionRecord } from "@/lib/api";


export function AssetVersionsTable({
  versions,
}: {
  versions: AssetVersionRecord[];
}) {
  if (versions.length === 0) {
    // The route only renders this component when the API returned
    // 200, so an empty list here means "the asset has rows but
    // every version was filtered out somehow" — unusual but worth
    // a friendly message rather than a blank table.
    return (
      <p className="text-sm text-[var(--color-fg-muted)]">No ingests yet.</p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
          <tr className="border-b border-[var(--color-border)]">
            <th className="px-3 py-2 text-left font-medium">Version</th>
            <th className="px-3 py-2 text-left font-medium">Run</th>
            <th className="px-3 py-2 text-left font-medium">Ingested</th>
            <th className="px-3 py-2 text-left font-medium">Source</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v, i) => (
            <tr
              // ``(version_id, run_id)`` is the natural key from the
              // server but the same pair can repeat if a run is
              // re-ingested with the same version — append the index
              // to keep React keys unique without depending on the
              // server-side row id.
              key={`${v.version_id}-${v.run_id}-${i}`}
              data-testid="asset-version-row"
              data-version-id={v.version_id}
              data-run-id={v.run_id}
              className="border-b border-[var(--color-border)] hover:bg-[var(--color-bg-row)]"
            >
              <td
                className="px-3 py-2 font-mono text-xs"
                title={v.version_id}
              >
                {truncate(v.version_id, 16)}
              </td>
              <td className="px-3 py-2">
                <Link
                  href={`/runs/detail/?id=${encodeURIComponent(v.run_id)}`}
                  className="font-mono text-xs text-[var(--color-accent)] hover:underline"
                  title={v.run_id}
                >
                  {truncate(v.run_id, 18)}
                </Link>
              </td>
              <td className="px-3 py-2 text-xs text-[var(--color-fg-muted)]" title={v.ingested_at}>
                {fmtTime(v.ingested_at)}
              </td>
              <td className="px-3 py-2">
                <Badge tone={v.source === "otlp" ? "info" : "muted"}>{v.source}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function truncate(s: string, n: number): string {
  if (!s) return "";
  if (s.length <= n) return s;
  // ``…`` U+2026 (the proper ellipsis, not three dots) so it
  // measures as one character in narrow columns.
  return s.slice(0, n) + "…";
}


function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
