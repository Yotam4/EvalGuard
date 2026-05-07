"use client";

/**
 * React Query provider — single client per app instance, mounted
 * inside the root layout.  Tuned defaults:
 *
 * - ``staleTime: 30s`` — runs are immutable artifacts; refetching
 *   the run-detail page every navigation is wasteful.
 * - ``retry: 1`` — server-side 5xx is more often a transient
 *   reroute than a real failure; one retry is cheap.
 * - ``refetchOnWindowFocus: false`` — admin tools spend most of
 *   their time backgrounded; the focus-refetch default produces
 *   noisy network traffic for negligible UX gain.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

export function QueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
