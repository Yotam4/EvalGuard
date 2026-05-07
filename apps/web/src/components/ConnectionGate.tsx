"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { isConfigured } from "@/lib/auth";
import { Card } from "./Card";

/**
 * Wrap a page that needs the API. If no server URL + token are
 * configured in localStorage, render a hint that links to Settings
 * instead of letting the page surface a confusing fetch error.
 *
 * Reads ``localStorage`` only after mount (the static export
 * prerenders pages at build time when ``window`` doesn't exist).
 */
export function ConnectionGate({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [ok, setOk] = useState(false);

  useEffect(() => {
    setOk(isConfigured());
    setReady(true);
  }, []);

  if (!ready) return null;
  if (!ok) {
    return (
      <Card title="Server not configured">
        <p className="text-sm text-[var(--color-fg-muted)]">
          Set the EvalGuard server URL and an API token before browsing
          runs.
        </p>
        <Link
          href="/settings"
          className="mt-3 inline-block rounded bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-[var(--color-bg)]"
        >
          Open Settings →
        </Link>
      </Card>
    );
  }
  return <>{children}</>;
}
