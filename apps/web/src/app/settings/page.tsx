"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { Card } from "@/components/Card";
import { Badge } from "@/components/Badge";
import {
  clearAuth, getServerUrl, getToken, setServerUrl, setToken,
} from "@/lib/auth";
import { health, type Health } from "@/lib/api";

/**
 * Settings page — server URL + token + connectivity probe.
 *
 * The static-export bundle has no compile-time config for the
 * server URL; the operator pastes it here and it persists in
 * ``localStorage`` so subsequent navigations / page reloads
 * keep using it.
 *
 * The "Test connection" button hits ``GET /v1/health`` (the only
 * unauthenticated endpoint) and surfaces whatever the server
 * advertises, including ``mode: open`` so the operator notices a
 * misconfigured prod deployment.
 */
export default function SettingsPage() {
  const [url, setUrl]   = useState("");
  const [tok, setTok]   = useState("");
  const [savedFlash, setSavedFlash] = useState(false);

  // Load existing values on mount (SSR-safe).
  useEffect(() => {
    setUrl(getServerUrl() ?? "");
    setTok(getToken() ?? "");
  }, []);

  const probe = useMutation<Health>({ mutationFn: () => health() });

  const onSave = () => {
    setServerUrl(url.trim());
    setToken(tok.trim());
    setSavedFlash(true);
    setTimeout(() => setSavedFlash(false), 1500);
  };

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Settings</h1>

      <Card title="Server connection">
        <div className="space-y-4">
          <Field
            label="Server URL"
            placeholder="https://evalguard.example.com"
            value={url}
            onChange={setUrl}
          />
          <Field
            label="API token"
            placeholder="evk_…"
            value={tok}
            onChange={setTok}
            type="password"
          />
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onSave}
              className="rounded bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-[var(--color-bg)] hover:opacity-90"
            >
              Save
            </button>
            <button
              type="button"
              onClick={() => probe.mutate()}
              disabled={!url || probe.isPending}
              className="rounded border border-[var(--color-border)] px-3 py-1.5 text-sm hover:bg-[var(--color-bg-row)] disabled:opacity-40"
            >
              {probe.isPending ? "Probing…" : "Test connection"}
            </button>
            <button
              type="button"
              onClick={() => { clearAuth(); setUrl(""); setTok(""); }}
              className="rounded border border-[var(--color-border)] px-3 py-1.5 text-sm text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-row)]"
            >
              Forget
            </button>
            {savedFlash && (
              <span className="text-sm text-[var(--color-pass)]">Saved.</span>
            )}
          </div>
        </div>
      </Card>

      {(probe.data || probe.error) && (
        <Card title="Server status">
          {probe.data && <HealthSummary data={probe.data} />}
          {probe.error && (
            <p className="text-sm text-[var(--color-fail)]">
              {probe.error instanceof Error
                ? probe.error.message
                : String(probe.error)}
            </p>
          )}
        </Card>
      )}

      <Card title="Notes">
        <ul className="space-y-1 text-sm text-[var(--color-fg-muted)]">
          <li>· The token never leaves your browser; it lives in
              <code className="mx-1 rounded bg-[var(--color-bg-row)] px-1 py-0.5">localStorage</code>.</li>
          <li>· The server&apos;s
              <code className="mx-1 rounded bg-[var(--color-bg-row)] px-1 py-0.5">EVALGUARD_CORS_ORIGINS</code>
              must include this UI&apos;s origin.</li>
          <li>· In <code className="mx-1 rounded bg-[var(--color-bg-row)] px-1 py-0.5">open</code> mode the
              token is unused; the server warns at startup.</li>
        </ul>
      </Card>
    </div>
  );
}


function HealthSummary({ data }: { data: Health }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
      <dt className="text-[var(--color-fg-muted)]">status</dt>
      <dd><Badge tone="pass">{data.status}</Badge></dd>
      <dt className="text-[var(--color-fg-muted)]">mode</dt>
      <dd>
        <Badge tone={data.mode === "open" ? "warn" : "info"}>{data.mode}</Badge>
      </dd>
      <dt className="text-[var(--color-fg-muted)]">db</dt>
      <dd className="font-mono text-xs">{data.db}</dd>
      <dt className="text-[var(--color-fg-muted)]">version</dt>
      <dd className="font-mono text-xs">{data.version}</dd>
    </dl>
  );
}


function Field({
  label, value, onChange, placeholder, type = "text",
}: {
  label: string;
  value: string;
  onChange: (s: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="block">
      <span className="block text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
        {label}
      </span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
      />
    </label>
  );
}
