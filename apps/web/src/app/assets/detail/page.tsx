"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { ConnectionGate } from "@/components/ConnectionGate";
import { AssetVersionsTable } from "@/components/AssetVersionsTable";
import { listAssetVersions, type AssetKind } from "@/lib/api";


/**
 * /assets/detail — drill-down for one asset.
 *
 * URL shape: ``?kind=judge&asset_id=q&project_id=proj_xxx``.
 * Three query params live on the URL so the page is shareable;
 * React Query's cache key picks them up naturally.
 *
 * When any required param is missing, we render a friendly prompt
 * pointing back to ``/assets`` rather than a generic empty state —
 * the URL contract is the affordance here.
 */
export default function AssetDetailPage() {
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
  const kind       = params.get("kind") as AssetKind | null;
  const assetId    = params.get("asset_id");
  const projectId  = params.get("project_id");

  if (!kind || !assetId || !projectId) {
    return (
      <Card title="Missing parameters">
        <p className="text-sm text-[var(--color-fg-muted)]">
          The asset detail page needs <code>kind</code>,{" "}
          <code>asset_id</code>, and <code>project_id</code> on the
          URL.
        </p>
        <Link
          href="/assets"
          className="mt-3 inline-block rounded border border-[var(--color-border)] px-3 py-1.5 text-sm hover:bg-[var(--color-bg-row)]"
        >
          ← Back to assets
        </Link>
      </Card>
    );
  }
  return <Detail kind={kind} assetId={assetId} projectId={projectId} />;
}


function Detail({
  kind, assetId, projectId,
}: { kind: AssetKind; assetId: string; projectId: string }) {
  const q = useQuery({
    queryKey: ["asset-versions", kind, assetId, projectId],
    queryFn:  () => listAssetVersions(kind, assetId, projectId),
    refetchOnWindowFocus: false,
  });

  if (q.isPending)
    return <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>;
  if (q.error)
    return (
      <p className="text-sm text-[var(--color-fail)]">
        {q.error instanceof Error ? q.error.message : String(q.error)}
      </p>
    );
  if (!q.data) return null;

  const r = q.data;
  // Distinct version_ids — shown as a quick chip count in the header
  // so the operator knows at a glance whether the asset has churned.
  const distinctVersions = new Set(r.versions.map((v) => v.version_id)).size;

  return (
    <div className="space-y-4">
      <div>
        <Link
          href="/assets"
          className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
        >
          ← Assets
        </Link>
        <div className="mt-1 flex flex-wrap items-baseline gap-3">
          <h1 className="font-mono text-lg font-semibold">{r.asset_id}</h1>
          <Badge tone="muted">{r.kind}</Badge>
          <span className="text-sm text-[var(--color-fg-muted)]">
            project{" "}
            <Link
              href={`/runs/?project=${encodeURIComponent(r.project_name)}`}
              className="text-[var(--color-accent)] hover:underline"
            >
              {r.project_name}
            </Link>
          </span>
          <Badge tone="info">{distinctVersions} version{distinctVersions === 1 ? "" : "s"}</Badge>
          <Badge tone="info">{r.versions.length} ingest{r.versions.length === 1 ? "" : "s"}</Badge>
        </div>
      </div>

      <Card title="Versions">
        <AssetVersionsTable versions={r.versions} />
      </Card>
    </div>
  );
}
