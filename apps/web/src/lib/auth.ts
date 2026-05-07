/**
 * Bearer-token + server-URL persistence in ``localStorage``.
 *
 * The static-export bundle has no build-time URL — the operator
 * supplies both at runtime via the Settings page so the same
 * artifact deploys against staging and prod without rebuilding.
 *
 * SSR-safe: every accessor guards on ``typeof window`` so the
 * Next build (which prerenders pages at compile time for static
 * export) doesn't crash on missing ``localStorage``.
 */

const SERVER_KEY = "evalguard.server_url";
const TOKEN_KEY = "evalguard.api_token";

export function getServerUrl(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(SERVER_KEY);
}

export function setServerUrl(url: string): void {
  if (typeof window === "undefined") return;
  // Strip trailing slash so the API client can append paths cleanly.
  window.localStorage.setItem(SERVER_KEY, url.replace(/\/$/, ""));
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(SERVER_KEY);
  window.localStorage.removeItem(TOKEN_KEY);
}

/** True iff both pieces are configured. */
export function isConfigured(): boolean {
  return Boolean(getServerUrl() && getToken());
}
