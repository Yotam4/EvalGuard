"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Badge } from "@/components/Badge";
import { Card } from "@/components/Card";
import { ConnectionGate } from "@/components/ConnectionGate";
import {
  ApiError,
  getLatestProjectConfig, getProjectConfigHistory, getProjectConfigRevision,
  pushProjectConfig,
  type ProjectConfig, type ProjectConfigSummary,
} from "@/lib/api";


/**
 * /config/ — PROXY-4: per-project config viewer + editor.
 *
 * URL: ``/config/?project=customer-service&rev=<id>``
 *
 * - ``project`` (required) — which project's config to show
 * - ``rev``      — specific revision to view (omit for latest)
 *
 * Two surfaces in one page:
 * - Editor (top) — view + edit the current revision's bytes.
 *   "Save" POSTs to the same endpoint ``evalguard push-config``
 *   uses.  Same server-side validation, same SHA-256 idempotency
 *   (re-saving identical bytes returns 200, lands no new row).
 * - History (right rail) — list of prior revisions newest-first.
 *   Click a revision to view it; "Restore" copies its content into
 *   the editor so the operator can re-save it as the new latest.
 *
 * No Monaco / CodeMirror — a plain textarea with a monospace font
 * keeps the bundle small and matches the rest of the UI's
 * intentional minimalism.  Server-side validation produces a clear
 * 422 with the validation error in ``detail``; the editor surfaces
 * it inline.
 */
export default function ConfigPage() {
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
  const revParam = params.get("rev");
  const revId = revParam ? Number(revParam) : null;

  if (!project) return <ProjectPicker />;

  return (
    <div className="space-y-4">
      <Header project={project} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
        <EditorPanel project={project} revId={revId} />
        <HistoryPanel
          project={project}
          selectedRev={revId}
          onSelect={(id) => {
            const qs = new URLSearchParams(params);
            if (id === null) qs.delete("rev");
            else             qs.set("rev", String(id));
            router.replace(`/config/?${qs.toString()}`);
          }}
        />
      </div>
    </div>
  );
}


function Header({ project }: { project: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-3">
      <h1 className="text-xl font-semibold">Project config</h1>
      <span className="text-sm text-[var(--color-fg-muted)]">
        project <span className="text-[var(--color-fg)]">{project}</span>
      </span>
      <Link
        href={`/calls/?project=${encodeURIComponent(project)}`}
        className="ml-auto text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
      >
        ← back to calls
      </Link>
    </div>
  );
}


function ProjectPicker() {
  return (
    <Card title="Project config">
      <p className="text-sm text-[var(--color-fg-muted)]">
        The config editor is per-project.  Pass{" "}
        <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">
          ?project=&lt;slug&gt;
        </code>{" "}
        on the URL to open it.
      </p>
    </Card>
  );
}


function EditorPanel({
  project, revId,
}: {
  project: string;
  revId: number | null;
}) {
  // When ``revId`` is set we fetch the specific revision; otherwise
  // the latest.  The two endpoints return the same ``ProjectConfig``
  // shape so the editor doesn't branch on which one served it.
  const q = useQuery<ProjectConfig | null>({
    queryKey: ["config", project, revId ?? "latest"],
    queryFn: async () => {
      try {
        return revId === null
          ? await getLatestProjectConfig(project)
          : await getProjectConfigRevision(project, revId);
      } catch (e) {
        // 404 on "no config pushed yet" is an empty-state, not an
        // error — surface it as ``null`` so the editor renders the
        // first-push UX instead of an error toast.  Round-7 review-
        // pass: match by HTTP status, not a regex on the detail
        // string.  A future server change to the 404 wording (or a
        // localised "Not found" → "Introuvable") would silently
        // demote a real 404 into a thrown error otherwise.
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
  });

  const [draft, setDraft] = useState<string>("");
  const [dirty, setDirty] = useState<boolean>(false);
  const [pushError, setPushError] = useState<string | null>(null);
  // PROXY-4 review-pass: track the last revision we synced into the
  // editor so the effect below can tell "the operator changed the
  // ``rev=`` param" from "react-query re-fetched the same revision
  // (e.g. after a save invalidated the latest-config query)".  Only
  // the former should reset the draft — overwriting on a same-id
  // re-fetch would silently wipe edits made between save completion
  // and refetch resolution.  ``null`` is the chain-origin id for
  // the empty-state branch so re-renders of an empty project don't
  // re-seed the template on top of operator edits.
  const lastSyncedRevIdRef = useRef<number | null | "uninit">("uninit");

  // Sync the editor buffer when a new revision lands.  Resetting
  // ``dirty`` on revision change means switching to "view a prior
  // revision" doesn't strand the operator's edits across the swap.
  useEffect(() => {
    if (q.isPending) return;
    const incomingId = q.data?.id ?? null;
    if (lastSyncedRevIdRef.current === incomingId) {
      // Same revision as last sync — likely a same-id refetch (post-
      // save invalidation, background poll).  Do NOT overwrite the
      // operator's in-progress edits.
      return;
    }
    if (q.data) {
      setDraft(q.data.content);
      setDirty(false);
      setPushError(null);
    } else if (q.data === null) {
      // No revisions yet — seed the editor with a minimal template
      // so the first push is one keystroke from succeeding.
      setDraft(
        `version: 1\n` +
        `project: ${project}\n` +
        `providers:\n  - id: 'mock:m'\n    config:\n      mode: echo\n`,
      );
      setDirty(true);   // template needs to be saved
      setPushError(null);
    }
    lastSyncedRevIdRef.current = incomingId;
  }, [q.data, q.isPending, project]);

  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: () => pushProjectConfig(project, draft),
    onSuccess: () => {
      setPushError(null);
      // Invalidate the two queries the page depends on so the
      // history list refreshes with the new revision and the
      // editor re-reads the latest.
      qc.invalidateQueries({ queryKey: ["config", project] });
      qc.invalidateQueries({ queryKey: ["config-history", project] });
    },
    onError: (e) => {
      // Round-7 review-pass: use the server's clean ``detail``
      // string when it's an ApiError (e.g. a 422 with multi-line
      // YAML parse output like "Line 14, column 3: …").  Falling
      // back to ``message`` produced ``"422: detail"`` collapsed
      // onto one line — unreadable for structured YAML errors.
      if (e instanceof ApiError) setPushError(e.detail);
      else if (e instanceof Error) setPushError(e.message);
      else setPushError(String(e));
    },
  });

  // Round-7 review-pass: clear the "Saved · sha256 …" confirmation
  // chip ~10s after the save lands.  Without this the chip persists
  // forever (until the next mutation or page reload), so an operator
  // returning to the tab tomorrow sees a stale "Saved" indicator
  // attached to a draft they haven't touched.  ``m.reset()`` returns
  // the mutation to idle so ``m.isSuccess`` flips back to false.
  useEffect(() => {
    if (!m.isSuccess) return;
    const t = setTimeout(() => m.reset(), 10_000);
    return () => clearTimeout(t);
  }, [m.isSuccess, m]);

  // Round-8 review-pass: warn before tab close / hard reload when the
  // editor has unsaved bytes.  Without this an operator paste-and-
  // walk-away loses the entire draft to a stray ⌘W with no recourse
  // (the server has no auto-save and the textarea isn't persisted to
  // local storage).  Client-side route changes within the SPA are
  // handled by the ``lastSyncedRevIdRef`` anti-clobber upstream — this
  // guard covers the browser-level exit only.
  useEffect(() => {
    if (!dirty) return;
    function onBeforeUnload(e: BeforeUnloadEvent) {
      // Modern browsers ignore the custom string and show their own
      // dialog, but ``preventDefault`` + ``returnValue`` are still
      // required to opt into the prompt at all.
      e.preventDefault();
      e.returnValue = "";
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  if (q.isPending) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fg-muted)]">Loading config…</p>
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

  const isNewProject = q.data === null;
  const meta = q.data;

  return (
    <Card
      title={isNewProject ? "First push" : `Revision ${meta!.id}`}
    >
      {meta && (
        <div
          data-testid="config-meta"
          className="mb-3 flex flex-wrap gap-3 text-xs text-[var(--color-fg-muted)]"
        >
          <span title={meta.content_sha256}>
            sha256: <code>{meta.content_sha256.slice(0, 12)}…</code>
          </span>
          <span>by <code>{meta.pushed_by}</code></span>
          <span title={meta.pushed_at}>{fmtTime(meta.pushed_at)}</span>
          {dirty && <Badge tone="info">unsaved</Badge>}
        </div>
      )}
      <textarea
        data-testid="config-editor"
        spellCheck={false}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          setDirty(true);
          setPushError(null);
        }}
        rows={24}
        className="block w-full resize-y rounded border border-[var(--color-border)] bg-[var(--color-bg)] p-2 font-mono text-xs leading-tight text-[var(--color-fg)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]"
      />
      {pushError && (
        // Round-7 review-pass: ``<pre>`` + whitespace-pre-wrap so a
        // multi-line YAML parse error (line/column markers, the
        // server's structured 422 message) renders readably.  A
        // plain ``<p>`` was collapsing newlines and producing
        // unreadable run-on strings for the common ``yaml.YAMLError``
        // case.  ``font-mono`` keeps line/column alignment intact.
        <pre
          data-testid="config-push-error"
          role="alert"
          className="mt-2 whitespace-pre-wrap break-words rounded border border-[var(--color-fail)] bg-[var(--color-bg-row)] p-2 font-mono text-xs text-[var(--color-fail)]"
        >
          {pushError}
        </pre>
      )}
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          data-testid="config-save"
          onClick={() => m.mutate()}
          disabled={!dirty || m.isPending}
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-row)] px-3 py-1 text-sm hover:bg-[var(--color-bg-card)] disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-accent)]"
        >
          {m.isPending ? "Saving…" : isNewProject ? "Push first revision" : "Save as new revision"}
        </button>
        {meta && dirty && (
          <button
            type="button"
            onClick={() => {
              setDraft(meta.content);
              setDirty(false);
              setPushError(null);
            }}
            className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
          >
            Discard
          </button>
        )}
        {m.isSuccess && !dirty && (
          <span
            data-testid="config-save-ok"
            className="text-xs text-[var(--color-pass)]"
          >
            Saved · sha256 {m.data.content_sha256.slice(0, 12)}…
          </span>
        )}
      </div>
    </Card>
  );
}


function HistoryPanel({
  project, selectedRev, onSelect,
}: {
  project: string;
  selectedRev: number | null;
  onSelect: (id: number | null) => void;
}) {
  const q = useQuery({
    queryKey: ["config-history", project],
    queryFn: () => getProjectConfigHistory(project, { limit: 20 }),
  });

  if (q.isPending) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fg-muted)]">Loading history…</p>
      </Card>
    );
  }
  // 404 on a brand-new project is fine — the editor handles the
  // empty state.  Surface other errors loudly.  Round-7 review-pass:
  // match by HTTP status so a server wording change (or a localised
  // detail) doesn't silently swallow a real error.
  if (q.error && !(q.error instanceof ApiError && q.error.status === 404)) {
    return (
      <Card>
        <p className="text-sm text-[var(--color-fail)]">
          {(q.error as Error).message}
        </p>
      </Card>
    );
  }
  const configs: ProjectConfigSummary[] = q.data?.configs ?? [];
  if (configs.length === 0) {
    return (
      <Card title="History">
        <p className="text-sm text-[var(--color-fg-muted)]">No revisions yet.</p>
      </Card>
    );
  }

  return (
    <Card title="History">
      <ul data-testid="config-history" className="space-y-1">
        {configs.map((c, i) => {
          const isLatest = i === 0;
          // ``selectedRev === null`` means "viewing latest"; treat
          // that as the latest entry highlighted so the user sees
          // their context unambiguously.
          const active = selectedRev === c.id || (selectedRev === null && isLatest);
          return (
            <li key={c.id}>
              <button
                type="button"
                data-testid="config-history-row"
                data-rev-id={c.id}
                aria-pressed={active}
                onClick={() => onSelect(isLatest ? null : c.id)}
                className={
                  "block w-full rounded border px-2 py-1 text-left text-xs transition " +
                  (active
                    ? "border-[var(--color-accent)] bg-[var(--color-bg-row)]"
                    : "border-[var(--color-border)] hover:bg-[var(--color-bg-row)]")
                }
              >
                <div className="flex items-baseline gap-2">
                  <span className="font-mono">#{c.id}</span>
                  {isLatest && <Badge tone="info">latest</Badge>}
                  <span
                    className="ml-auto text-[var(--color-fg-muted)]"
                    title={c.pushed_at}
                  >
                    {fmtTime(c.pushed_at)}
                  </span>
                </div>
                <div className="mt-0.5 text-[var(--color-fg-muted)]">
                  <code>{c.content_sha256.slice(0, 12)}…</code>
                  {" · "}
                  by <code>{c.pushed_by}</code>
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}


function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
