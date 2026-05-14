import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setServerUrl, setToken } from "../auth";
import {
  ApiError, NotConfiguredError,
  createApiKey, createOrg, getRun, getReviewQueue, getRunDrift,
  health, listAssets, listRunReviews, listRuns,
  revokeApiKey, submitReview,
} from "../api";

/**
 * The API client carries every server-facing concern: bearer
 * injection, error translation, query-string assembly. These tests
 * pin each one against a stubbed fetch so the contract stays stable
 * even if the server moves.
 */

describe("api client", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.localStorage.clear();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // ---------------------------------------------------------------------
  // configuration

  it("throws NotConfiguredError when server URL is missing", async () => {
    await expect(health()).rejects.toBeInstanceOf(NotConfiguredError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------
  // health


  it("does NOT attach the bearer to /v1/health", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_secret");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ status: "ok", version: "0.0.1", mode: "auth", db: "sqlite" }),
    });
    const data = await health();
    expect(data.mode).toBe("auth");
    const [, init] = fetchMock.mock.calls[0];
    // /v1/health is unauthenticated by design (LB-friendly). The
    // client must not send the bearer for this endpoint.
    expect((init.headers as Record<string, string>)["Authorization"]).toBeUndefined();
  });

  // ---------------------------------------------------------------------
  // bearer + URL composition

  it("injects bearer + correct URL on authenticated calls", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_secret");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ runs: [], next: null }),
    });
    await listRuns({ project: "demo", limit: 5 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/v1/runs?project=demo&limit=5");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer evk_secret");
  });

  it("URL-encodes path segments to defend against odd run_ids", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_secret");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({}),
    });
    await getRun("run id with spaces");
    const [url] = fetchMock.mock.calls[0];
    // The space becomes %20; without encoding, fetch would split on
    // it and produce an invalid URL.
    expect(url).toContain("run%20id%20with%20spaces");
  });

  // ---------------------------------------------------------------------
  // error translation

  it("turns 4xx with JSON detail into ApiError carrying the detail", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: false, status: 409, statusText: "Conflict",
      json: async () => ({ detail: "Org slug 'acme' already exists." }),
    });
    try {
      await createOrg({ slug: "acme", name: "Acme" });
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      const err = e as ApiError;
      expect(err.status).toBe(409);
      expect(err.detail).toContain("already exists");
    }
  });

  it("falls back to statusText when the body is not JSON", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: false, status: 502, statusText: "Bad Gateway",
      json: async () => { throw new Error("not json"); },
    });
    try {
      await listRuns();
      throw new Error("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      const err = e as ApiError;
      expect(err.status).toBe(502);
      expect(err.detail).toBe("Bad Gateway");
    }
  });

  // ---------------------------------------------------------------------
  // 204 handling


  it("returns undefined on 204 (DELETE responses)", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 204,
      // Calling .json() on a 204 throws — the client must not call it.
      json: async () => { throw new Error("no body on 204"); },
    });
    const result = await revokeApiKey("key_abc");
    expect(result).toBeUndefined();
  });

  // ---------------------------------------------------------------------
  // POST body shape


  it("createApiKey defaults scopes to []", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        key: { key_id: "key_x", org_id: "org_default", prefix: "evk_a",
               name: "ci", scopes: [], created_at: "2026-01-01",
               revoked_at: null, last_used_at: null },
        token: "evk_xxx",
      }),
    });
    await createApiKey("org_default", { name: "ci" });
    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse((init.body as string));
    expect(body.scopes).toEqual([]);
    expect(body.name).toBe("ci");
  });

  // ---------------------------------------------------------------------
  // listAssets — query-string assembly


  it("listAssets composes kind + project + limit query params", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ assets: [] }),
    });
    await listAssets({ kind: "judge", project: "demo", limit: 25 });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/assets?kind=judge&project=demo&limit=25",
    );
  });

  // ---------------------------------------------------------------------
  // timeout

  it("aborts the request after the default 30s timeout when fetch hangs", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    vi.useFakeTimers();
    // Resolve only when the per-request AbortSignal aborts.
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        });
      });
    });
    const promise = listRuns().catch((e) => e);
    // Drive past the 30s timeout window.
    await vi.advanceTimersByTimeAsync(31_000);
    const err = await promise;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
    expect((err as ApiError).detail).toContain("timed out");
    vi.useRealTimers();
  });

  it("listAssets omits the query string when no params are passed", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ assets: [] }),
    });
    await listAssets();
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/v1/assets");
  });

  // ---------------------------------------------------------------------
  // drift (Phase 3b)


  it("getRunDrift composes the vs= query and URL-encodes both ids", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        current_run_id:  "run_a/with slash",
        baseline_run_id: "run_b",
        alpha:    0.05,
        metrics:  [],
        skipped:  [],
      }),
    });
    await getRunDrift("run_a/with slash", "run_b");
    const [url] = fetchMock.mock.calls[0];
    // Path segment must be percent-encoded (the slash would otherwise
    // be read by the router as a path separator); the ``vs`` query
    // value goes through URLSearchParams.
    expect(url).toBe(
      "https://api.example.com/v1/runs/run_a%2Fwith%20slash/drift?vs=run_b",
    );
  });


  it("getRunDrift forwards alpha when supplied", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        current_run_id: "run_a", baseline_run_id: "run_b",
        alpha: 0.01, metrics: [], skipped: [],
      }),
    });
    await getRunDrift("run_a", "run_b", { alpha: 0.01 });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/runs/run_a/drift?vs=run_b&alpha=0.01",
    );
  });


  // ---------------------------------------------------------------------
  // reviews (Phase 4)


  it("getReviewQueue puts run_id into the query string (no path segment)", async () => {
    // The server's route is ``/v1/reviews/queue?run_id=`` (a flat
    // resource), NOT ``/v1/runs/{id}/reviews/queue``. Pin the URL
    // so a refactor that confuses the two doesn't silently 404.
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ items: [], run_id: "run_a" }),
    });
    await getReviewQueue("run_a", { limit: 10 });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/reviews/queue?run_id=run_a&limit=10",
    );
  });


  it("submitReview POSTs the verdict body", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        id: 1, run_id: "run_a", row_id: "r1", project_id: "proj_x",
        reviewer_key_id: "key_a", verdict: "override_pass",
        note: "fp", created_at: "2026-05-14T00:00:00",
        updated_at: "2026-05-14T00:00:00",
      }),
    });
    await submitReview({
      run_id: "run_a", row_id: "r1",
      verdict: "override_pass", note: "fp",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/v1/reviews");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      run_id: "run_a", row_id: "r1",
      verdict: "override_pass", note: "fp",
    });
  });


  it("listRunReviews encodes the run_id path segment", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ reviews: [] }),
    });
    await listRunReviews("run with spaces");
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/runs/run%20with%20spaces/reviews",
    );
  });
});
