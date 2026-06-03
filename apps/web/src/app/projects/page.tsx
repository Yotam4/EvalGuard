"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { ConnectionGate } from "@/components/ConnectionGate";
import { createProject, fmtError, listProjects, type Project } from "@/lib/api";

/**
 * /projects — list projects in the caller's org and create new
 * ones. Org scoping is implicit (the server uses the bearer's
 * org_id); admin callers can target a different org via the
 * server's ``?org_id=`` query param, exposed here as an optional
 * input.
 */
export default function ProjectsPage() {
  return (
    <ConnectionGate>
      <Inner />
    </ConnectionGate>
  );
}


function Inner() {
  const qc = useQueryClient();

  const [orgFilter, setOrgFilter] = useState("");
  const list = useQuery({
    queryKey: ["projects", orgFilter || null],
    queryFn: () => listProjects({ org_id: orgFilter || undefined }),
  });

  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: (vars: { slug: string; name?: string; org_id?: string }) =>
      createProject(vars),
    onSuccess: () => {
      setSlug("");
      setName("");
      setErrMsg(null);
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (e: Error) =>
      setErrMsg(fmtError(e)),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Projects</h1>

      <Card
        title="Projects in scope"
        action={
          <input
            value={orgFilter}
            onChange={(e) => setOrgFilter(e.target.value)}
            placeholder="org_id (admin only)"
            className="rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
          />
        }
      >
        <div className="min-h-[200px]">
          {list.isPending && <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>}
          {list.error && (
            <p className="text-sm text-[var(--color-fail)]">
              {fmtError(list.error)}
            </p>
          )}
          {list.data && <ProjectsTable projects={list.data.projects} />}
        </div>
      </Card>

      <Card title="Create new project">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!slug.trim()) return;
            create.mutate({
              slug: slug.trim(),
              name: name.trim() || undefined,
              org_id: orgFilter || undefined,
            });
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
                placeholder="demo"
                pattern="[a-z0-9][a-z0-9-]*"
                className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              />
            </label>
          </div>
          <div className="flex-1">
            <label className="block">
              <span className="block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
                Display name (optional)
              </span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Demo project"
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
          Slugs are unique per-org — two orgs can both have a project
          named <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">demo</code>.
        </p>
      </Card>
    </div>
  );
}


function ProjectsTable({ projects }: { projects: Project[] }) {
  if (projects.length === 0) {
    return <p className="text-sm text-[var(--color-fg-muted)]">No projects yet.</p>;
  }
  return (
    <div className="overflow-x-auto">
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        <tr className="border-b border-[var(--color-border)]">
          <th className="px-2 py-1.5 text-left font-medium">Slug</th>
          <th className="px-2 py-1.5 text-left font-medium">Name</th>
          <th className="px-2 py-1.5 text-left font-medium">Org</th>
          <th className="px-2 py-1.5 text-left font-medium">Project id</th>
          <th className="px-2 py-1.5 text-left font-medium">Runs</th>
        </tr>
      </thead>
      <tbody>
        {projects.map((p) => (
          <tr key={p.project_id} className="border-b border-[var(--color-border)]">
            <td className="px-2 py-1.5 font-mono text-xs">{p.slug}</td>
            <td className="px-2 py-1.5">{p.name}</td>
            <td className="px-2 py-1.5 font-mono text-xs text-[var(--color-fg-muted)]">{p.org_id}</td>
            <td className="px-2 py-1.5 font-mono text-xs text-[var(--color-fg-muted)]">{p.project_id}</td>
            <td className="px-2 py-1.5">
              <Link
                href={`/runs/?project=${encodeURIComponent(p.slug)}`}
                className="text-xs text-[var(--color-accent)] hover:underline"
              >
                view runs →
              </Link>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
}
