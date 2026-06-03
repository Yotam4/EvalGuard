"use client";

import { useEffect } from "react";

/**
 * Route-segment error boundary.  Without this, a runtime error in a
 * page (e.g. an unexpected response shape from the API, an undefined
 * accessed during render) falls through to Next.js's default error
 * page — light-mode chrome on top of the dark app shell, no
 * actionable affordance.
 *
 * Renders inside the root layout, so the top nav and EvalGuard
 * brand stay visible and the user can navigate away from the
 * broken page without a hard reload.
 */
export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  // Surface to the browser console once — gives debuggers the
  // original stack without spamming logs on re-render.
  useEffect(() => {
    if (typeof console !== "undefined") {
      console.error("Route-segment error:", error);
    }
  }, [error]);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Something went wrong on this page</h1>
      <p className="text-sm text-[var(--color-fg-muted)]">
        The page hit a runtime error and stopped rendering.
        The rest of the app still works — try one of the buttons
        below, or use the top nav.
      </p>
      <pre
        role="alert"
        className="whitespace-pre-wrap break-words rounded border border-[var(--color-fail)] bg-[var(--color-bg-row)] p-3 font-mono text-xs text-[var(--color-fail)]"
      >
        {error.message || "(no message)"}
        {error.digest && `\n\ndigest: ${error.digest}`}
      </pre>
      <div className="flex flex-wrap gap-2 text-sm">
        <button
          type="button"
          onClick={reset}
          className="rounded border border-[var(--color-accent)] px-3 py-1.5 text-[var(--color-accent)] hover:bg-[var(--color-bg-row)]"
        >
          Try again
        </button>
        <a
          href="/runs"
          className="rounded border border-[var(--color-border)] px-3 py-1.5 hover:bg-[var(--color-bg-row)]"
        >
          ← Runs
        </a>
      </div>
    </div>
  );
}
