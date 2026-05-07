"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { ConnectionGate } from "@/components/ConnectionGate";
import { createOrg, listOrgs, type Org } from "@/lib/api";

/**
 * /orgs — list every visible org and (admin-only) create new ones.
 *
 * Listing is silently scoped server-side: a member sees only their
 * own org; an admin sees all. The "Create org" form posts to the
 * admin-gated endpoint, so a member submitting it gets 403, which
 * the UI surfaces inline rather than crashing.
 */
export default function OrgsPage() {
  return (
    <ConnectionGate>
      <Inner />
    </ConnectionGate>
  );
}


function Inner() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["orgs"], queryFn: () => listOrgs() });

  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [errMsg, setErrMsg] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: (vars: { slug: string; name: string }) => createOrg(vars),
    onSuccess: () => {
      setSlug("");
      setName("");
      setErrMsg(null);
      qc.invalidateQueries({ queryKey: ["orgs"] });
    },
    onError: (e: Error) => setErrMsg(e.message),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Organizations</h1>

      <Card title="All orgs">
        {list.isPending && <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}
        {list.error && (
          <p className="text-sm text-[var(--color-fail)]">
            {list.error instanceof Error ? list.error.message : String(list.error)}
          </p>
        )}
        {list.data && <OrgsTable orgs={list.data.orgs} />}
      </Card>

      <Card title="Create new org (admin)">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!slug.trim() || !name.trim()) return;
            create.mutate({ slug: slug.trim(), name: name.trim() });
          }}
        >
          <div className="flex-1">
            <label className="block">
              <span className="block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
                Slug
              </span>
              <input
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                placeholder="acme"
                pattern="[a-z0-9][a-z0-9-]*"
                className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              />
            </label>
          </div>
          <div className="flex-1">
            <label className="block">
              <span className="block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
                Display name
              </span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Acme Inc."
                className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              />
            </label>
          </div>
          <button
            type="submit"
            disabled={create.isPending}
            className="rounded bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-[var(--color-bg)] hover:opacity-90 disabled:opacity-40"
          >
            {create.isPending ? "Creating…" : "Create"}
          </button>
        </form>
        {errMsg && (
          <p className="mt-3 text-sm text-[var(--color-fail)]">{errMsg}</p>
        )}
        <p className="mt-3 text-xs text-[var(--color-fg-muted)]">
          Slugs are lowercase URL-safe identifiers (a–z, 0–9, hyphen).
          Org creation requires the <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">admin</code> scope.
        </p>
      </Card>
    </div>
  );
}


function OrgsTable({ orgs }: { orgs: Org[] }) {
  if (orgs.length === 0) {
    return <p className="text-sm text-[var(--color-fg-muted)]">No orgs yet.</p>;
  }
  return (
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        <tr className="border-b border-[var(--color-border)]">
          <th className="px-2 py-1.5 text-left font-medium">Slug</th>
          <th className="px-2 py-1.5 text-left font-medium">Name</th>
          <th className="px-2 py-1.5 text-left font-medium">Org id</th>
          <th className="px-2 py-1.5 text-left font-medium">Created</th>
        </tr>
      </thead>
      <tbody>
        {orgs.map((o) => (
          <tr key={o.org_id} className="border-b border-[var(--color-border)]">
            <td className="px-2 py-1.5 font-mono text-xs">{o.slug}</td>
            <td className="px-2 py-1.5">{o.name}</td>
            <td className="px-2 py-1.5 font-mono text-xs text-[var(--color-fg-muted)]">{o.org_id}</td>
            <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
              {new Date(o.created_at).toLocaleString()}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
