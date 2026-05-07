import { describe, it, expect, beforeEach } from "vitest";
import {
  clearAuth, getServerUrl, getToken, isConfigured,
  setServerUrl, setToken,
} from "../auth";

/**
 * Persistence and SSR-safety contract for the auth helpers.
 *
 * Every test starts with localStorage cleared (vitest.setup.ts
 * does this in afterEach). The ``isConfigured`` gate is the
 * single source of truth for "should the UI render or send the
 * caller to Settings", so its boundary cases are pinned tightly.
 */

describe("auth helpers", () => {
  beforeEach(() => window.localStorage.clear());

  it("returns null when no values are set", () => {
    expect(getServerUrl()).toBeNull();
    expect(getToken()).toBeNull();
    expect(isConfigured()).toBe(false);
  });

  it("setServerUrl persists and strips a trailing slash", () => {
    setServerUrl("https://api.example.com/");
    // Trailing slash is stripped so the API client can append paths
    // without ending up with a double slash.
    expect(getServerUrl()).toBe("https://api.example.com");
  });

  it("setToken persists verbatim", () => {
    setToken("evk_abc123");
    expect(getToken()).toBe("evk_abc123");
  });

  it("isConfigured needs BOTH a URL and a token", () => {
    setServerUrl("https://api.example.com");
    expect(isConfigured()).toBe(false);
    setToken("evk_x");
    expect(isConfigured()).toBe(true);
  });

  it("clearAuth wipes both keys", () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    clearAuth();
    expect(getServerUrl()).toBeNull();
    expect(getToken()).toBeNull();
    expect(isConfigured()).toBe(false);
  });
});
