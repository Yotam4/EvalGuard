import Link from "next/link";

/**
 * App-level 404 handler.  Without this Next.js renders its default
 * white-background "404 — Page not found" chrome on top of the app
 * shell, which (a) looks jarring against the dark theme and (b)
 * gives the user no orientation back to a real page.
 *
 * Static export still serves this file at the SPA's catch-all,
 * including any deep-link to a removed/renamed route.
 */
export default function NotFound() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="text-sm text-[var(--color-fg-muted)]">
        The path you opened isn&apos;t a route EvalGuard knows about.
        It may have been renamed, or the link may be stale.
      </p>
      <div className="flex flex-wrap gap-2 text-sm">
        <Link
          href="/runs"
          className="rounded border border-[var(--color-border)] px-3 py-1.5 hover:bg-[var(--color-bg-row)]"
        >
          ← Runs
        </Link>
        <Link
          href="/calls"
          className="rounded border border-[var(--color-border)] px-3 py-1.5 hover:bg-[var(--color-bg-row)]"
        >
          Calls
        </Link>
        <Link
          href="/settings"
          className="rounded border border-[var(--color-border)] px-3 py-1.5 hover:bg-[var(--color-bg-row)]"
        >
          Settings
        </Link>
      </div>
    </div>
  );
}
