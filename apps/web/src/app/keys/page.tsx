"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { ConfirmButton } from "@/components/ConfirmButton";
import { ConnectionGate } from "@/components/ConnectionGate";
import {
  fmtError,
  createApiKey, listApiKeys, listOrgs, revokeApiKey,
  type ApiKeyCreated, type ApiKeySummary,
} from "@/lib/api";

/**
 * /keys — manage API keys for an org.
 *
 * The org context comes from the user via a select populated by
 * ``listOrgs`` (members see only their own org; admins see all so
 * they can rotate keys cross-tenant).  Creating a key returns the
 * plaintext token exactly once — surfaced in a banner with a Copy
 * button, then never retrievable.  Revocation is two-click via
 * ``ConfirmButton``.
 */
export default function KeysPage() {
  return (
    <ConnectionGate>
      <Inner />
    </ConnectionGate>
  );
}


function Inner() {
  const qc = useQueryClient();
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: () => listOrgs() });

  const [orgId, setOrgId] = useState<string | null>(null);
  // Round-9 review-pass: seed ``orgId`` from the first visible org
  // exactly ONCE on data load instead of using a ternary fallback on
  // every render.  The fallback approach left ``orgId`` null while
  // ``effectiveOrg`` silently pointed at the first org — the
  // ``<select value={effectiveOrg ?? ""}>`` then showed blank to the
  // user (its empty value wasn't in the option list, so the browser
  // rendered nothing selected) while the Create form quietly
  // targeted the first org.  Selection invisible == data destruction
  // waiting to happen.  With the effect, the select value always
  // matches the operator's actual choice.
  useEffect(() => {
    if (orgId !== null) return;
    const firstOrg = orgs.data?.orgs[0]?.org_id;
    if (firstOrg) setOrgId(firstOrg);
  }, [orgs.data, orgId]);
  const effectiveOrg = orgId;

  const keys = useQuery({
    queryKey: ["keys", effectiveOrg],
    queryFn: () => listApiKeys(effectiveOrg!),
    enabled: !!effectiveOrg,
  });

  const [name, setName] = useState("");
  const [grantAdmin, setGrantAdmin] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [justCreated, setJustCreated] = useState<ApiKeyCreated | null>(null);

  const create = useMutation({
    mutationFn: (vars: { name: string; admin: boolean }) => {
      if (!effectiveOrg) throw new Error("No org selected.");
      return createApiKey(effectiveOrg, {
        name: vars.name,
        scopes: vars.admin ? ["admin"] : [],
      });
    },
    onSuccess: (data) => {
      setJustCreated(data);
      setName("");
      setGrantAdmin(false);
      setErrMsg(null);
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
    // Surface the server's clean ``detail`` (e.g. "Name already in
    // use") rather than the ``"422: detail"`` collapsed form
    // ``e.message`` produces.  The config editor already does this
    // — keys/projects/orgs were the stragglers.
    onError: (e: Error) =>
      setErrMsg(fmtError(e)),
  });

  const revoke = useMutation({
    mutationFn: (keyId: string) => revokeApiKey(keyId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["keys"] }),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">API keys</h1>

      <Card
        title="Org"
        action={
          <select
            value={effectiveOrg ?? ""}
            onChange={(e) => setOrgId(e.target.value || null)}
            className="rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
          >
            {/* Round-9 review-pass: explicit placeholder so the
                select can't quietly render a blank ``value`` while
                the browser auto-resolves to the first <option>.
                Without this, an admin landing during the orgs-list
                fetch saw a blank dropdown but ``effectiveOrg``
                already pointed at the first org — the Create form
                then targeted an org the user couldn't see selected.
                The disabled flag keeps the placeholder un-pickable
                so it can't re-enter the form. */}
            <option value="" disabled>
              {orgs.isPending ? "Loading orgs…" : "Select an org…"}
            </option>
            {orgs.data?.orgs.map((o) => (
              <option key={o.org_id} value={o.org_id}>{o.slug}</option>
            ))}
          </select>
        }
      >
        <p className="text-sm text-[var(--color-fg-muted)]">
          Keys live in a specific org. Switch the dropdown to manage
          a different one (admin only beyond your own).
        </p>
      </Card>

      {justCreated && (
        <NewKeyBanner data={justCreated} onDismiss={() => setJustCreated(null)} />
      )}

      <Card title="Create new key">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!name.trim() || !effectiveOrg) return;
            create.mutate({ name: name.trim(), admin: grantAdmin });
          }}
        >
          <div className="flex-1">
            <label className="block">
              <span className="block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
                Name (human label, not a secret)
              </span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="ci-prod"
                className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              />
            </label>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={grantAdmin}
              onChange={(e) => setGrantAdmin(e.target.checked)}
            />
            <span>admin scope</span>
          </label>
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
          Only admins can grant the <code className="rounded bg-[var(--color-bg-row)] px-1 py-0.5">admin</code> scope.
          The plaintext token is shown ONCE — copy it before you navigate away.
        </p>
      </Card>

      <Card title="Existing keys">
        <div className="min-h-[200px]">
          {!effectiveOrg && <p className="text-sm text-[var(--color-fg-muted)]">Pick an org above.</p>}
          {keys.isPending && effectiveOrg && (
            <p className="text-sm text-[var(--color-fg-muted)]">Loading…</p>
          )}
          {keys.error && (
            <p className="text-sm text-[var(--color-fail)]">
              {fmtError(keys.error)}
            </p>
          )}
          {keys.data && (
            <KeysTable
              keys={keys.data.keys}
              onRevoke={(id) => revoke.mutate(id)}
              revokingId={revoke.isPending ? (revoke.variables ?? null) : null}
            />
          )}
        </div>
      </Card>
    </div>
  );
}


function NewKeyBanner({
  data,
  onDismiss,
}: {
  data: ApiKeyCreated;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rounded-lg border border-[var(--color-warn)] bg-[color-mix(in_srgb,var(--color-warn)_10%,var(--color-bg-card))] p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-[var(--color-warn)]">
            New token created — visible exactly once
          </div>
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            Copy this now. The server never reveals the plaintext again.
            Reload the page or navigate away and the only path forward
            is creating a fresh key.
          </p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg)]"
          aria-label="Dismiss"
        >
          ✕
        </button>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <code className="break-all rounded bg-[var(--color-bg)] px-2 py-1.5 font-mono text-xs">
          {data.token}
        </code>
        <button
          type="button"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(data.token);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            } catch {
              // Some sandboxed browsers reject clipboard writes; fall
              // back to a manual prompt the user can copy from.
              window.prompt("Copy the token:", data.token);
            }
          }}
          className="rounded border border-[var(--color-border)] px-2 py-1.5 text-xs hover:bg-[var(--color-bg-row)]"
        >
          {copied ? "Copied!" : "Copy"}
        </button>
        <span className="text-xs text-[var(--color-fg-muted)]">
          for key <span className="font-mono">{data.key.key_id}</span> ({data.key.name})
        </span>
      </div>
    </div>
  );
}


function KeysTable({
  keys,
  onRevoke,
  revokingId,
}: {
  keys: ApiKeySummary[];
  onRevoke: (id: string) => void;
  revokingId: string | null;
}) {
  if (keys.length === 0) {
    return <p className="text-sm text-[var(--color-fg-muted)]">No keys yet.</p>;
  }
  return (
    <div className="overflow-x-auto">
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        <tr className="border-b border-[var(--color-border)]">
          <th className="px-2 py-1.5 text-left font-medium">Name</th>
          <th className="px-2 py-1.5 text-left font-medium">Prefix</th>
          <th className="px-2 py-1.5 text-left font-medium">Scopes</th>
          <th className="px-2 py-1.5 text-left font-medium">Last used</th>
          <th className="px-2 py-1.5 text-left font-medium">Status</th>
          <th className="px-2 py-1.5 text-right font-medium">Action</th>
        </tr>
      </thead>
      <tbody>
        {keys.map((k) => {
          const revoked = !!k.revoked_at;
          return (
            <tr key={k.key_id} className="border-b border-[var(--color-border)]">
              <td className="px-2 py-1.5">{k.name}</td>
              <td className="px-2 py-1.5 font-mono text-xs">{k.prefix}…</td>
              <td className="px-2 py-1.5 text-xs">
                {k.scopes.length > 0
                  ? k.scopes.map((s) => (
                      <Badge key={s} tone={s === "admin" ? "info" : "muted"}>{s}</Badge>
                    ))
                  : <span className="text-[var(--color-fg-muted)]">org-scoped</span>}
              </td>
              <td className="px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
                {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "—"}
              </td>
              <td className="px-2 py-1.5">
                {revoked
                  ? <Badge tone="fail">revoked</Badge>
                  : <Badge tone="pass">active</Badge>}
              </td>
              <td className="px-2 py-1.5 text-right">
                {!revoked && (
                  <ConfirmButton
                    onConfirm={() => onRevoke(k.key_id)}
                    label="Revoke"
                    confirmLabel="Click again to revoke"
                    pending={revokingId === k.key_id}
                  />
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
    </div>
  );
}
