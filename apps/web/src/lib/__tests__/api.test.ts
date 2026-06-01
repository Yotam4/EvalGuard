import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setServerUrl, setToken } from "../auth";
import {
  ApiError, NotConfiguredError,
  createApiKey, createOrg, getCallDetail, getRun, getReviewQueue, getRunDrift,
  health, listAssets, listAssetVersions, listGoldenCandidates,
  getLiveAggregate, listProjectCalls, listProjectLiveTimeline,
  listRunReviews, listRuns,
  promoteToGolden, revokeApiKey, submitReview, unPromoteGolden,
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


  it("listRuns forwards source filter when supplied", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ runs: [], next: null }),
    });
    await listRuns({ source: "otlp", limit: 10 });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/v1/runs?source=otlp&limit=10");
  });


  it("listRuns omits source when not supplied", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ runs: [], next: null }),
    });
    await listRuns({ limit: 50 });
    const [url] = fetchMock.mock.calls[0];
    // No ``source=`` substring — important so a misconfigured tab
    // state of ``null`` doesn't accidentally send ``source=null``.
    expect(url).toBe("https://api.example.com/v1/runs?limit=50");
    expect(url).not.toContain("source");
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


  // ---------------------------------------------------------------------
  // asset versions (Phase 2.6d)


  it("listAssetVersions encodes path segments AND puts project_id in the query string", async () => {
    // The server's URL is
    // ``/v1/assets/{kind}/{asset_id}/versions?project_id=…``; pin
    // both that the kind + asset_id are percent-encoded path
    // segments (a slash inside ``asset_id`` would otherwise be
    // read as a sub-resource) AND that ``project_id`` doesn't slip
    // into the path instead.
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        kind: "judge", asset_id: "q/strict",
        project_id: "proj_a", project_name: "demo",
        versions: [],
      }),
    });
    await listAssetVersions("judge", "q/strict", "proj_a", { limit: 50 });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/assets/judge/q%2Fstrict/versions"
      + "?project_id=proj_a&limit=50",
    );
  });


  it("listAssetVersions omits the limit when not supplied", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        kind: "dataset", asset_id: "g",
        project_id: "proj_a", project_name: "demo",
        versions: [],
      }),
    });
    await listAssetVersions("dataset", "g", "proj_a");
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/assets/dataset/g/versions?project_id=proj_a",
    );
  });


  // ---------------------------------------------------------------------
  // calls (Phase OBS)


  it("listProjectCalls composes tab + limit + source + opaque cursor", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ calls: [], next_cursor: null }),
    });
    await listProjectCalls("customer-service", {
      tab: "failures", limit: 25, source: "otlp",
      cursor: "eyJ0IjogIjIwMjYtMDUtMTUiLCAiaSI6IDQyfQ",
    });
    const [url] = fetchMock.mock.calls[0];
    // Tab is always emitted; cursor is passed through unchanged
    // (clients never construct it client-side).  Path slug is
    // percent-encoded so a slug with a slash doesn't break the
    // router.
    expect(url).toBe(
      "https://api.example.com/v1/projects/customer-service/calls"
      + "?tab=failures&cursor=eyJ0IjogIjIwMjYtMDUtMTUiLCAiaSI6IDQyfQ"
      + "&limit=25&source=otlp",
    );
  });


  it("listProjectCalls defaults tab to recent and omits unset params", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ calls: [], next_cursor: null }),
    });
    await listProjectCalls("demo");
    const [url] = fetchMock.mock.calls[0];
    // ``tab=recent`` is the default we always emit so the server
    // ``Literal`` validator never sees a missing value.  No other
    // params slip in.
    expect(url).toBe(
      "https://api.example.com/v1/projects/demo/calls?tab=recent",
    );
  });


  it("listProjectCalls percent-encodes the project slug path segment", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ calls: [], next_cursor: null }),
    });
    await listProjectCalls("weird slug/with bits");
    const [url] = fetchMock.mock.calls[0];
    expect(url).toContain(
      "/v1/projects/weird%20slug%2Fwith%20bits/calls?",
    );
  });


  it("getCallDetail encodes every path segment", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        run_id: "run_a", row_id: "r/1",
        trial_id: null, project_id: "proj", project: "demo",
        ingested_at: null, provider: null, model: null,
        passed: true, n_scores: 0, cost_usd: 0, latency_ms: 0,
        cache_hit: false, tags: [], input: null, expected: null,
        output: null, scores: [], trial_gates: [],
      }),
    });
    await getCallDetail("demo", "run_a", "r/1");
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/projects/demo/calls/run_a/r%2F1",
    );
  });


  // ---------------------------------------------------------------------
  // golden candidates (Phase OBS-4)


  it("promoteToGolden POSTs run_id + row_id + optional note", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        id: 1, run_id: "run_a", row_id: "r-1",
        project_id: "proj", promoted_by: "key_a",
        note: "edge case", created_at: "2026-05-15T07:30:00",
      }),
    });
    await promoteToGolden({ run_id: "run_a", row_id: "r-1", note: "edge case" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/v1/golden/candidates");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ run_id: "run_a", row_id: "r-1", note: "edge case" });
  });


  it("listGoldenCandidates targets the project's nested resource", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ candidates: [] }),
    });
    await listGoldenCandidates("customer-service", { limit: 50 });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/projects/customer-service/golden/candidates?limit=50",
    );
  });


  it("unPromoteGolden DELETEs by candidate id", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 204,
      // 204 has no body — the client must not call .json().
      json: async () => { throw new Error("204 has no body"); },
    });
    await unPromoteGolden(42);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/v1/golden/candidates/42");
    expect(init.method).toBe("DELETE");
  });


  // ---------------------------------------------------------------------
  // PROXY-2.5 — extended calls filters + live timeline / aggregate


  it("listProjectCalls threads tab=passed + from/to into the query string", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ calls: [], next_cursor: null }),
    });
    await listProjectCalls("demo", {
      tab: "passed",
      from: "2026-05-28T00:00:00+00:00",
      to:   "2026-05-29T00:00:00+00:00",
    });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/projects/demo/calls"
      + "?tab=passed"
      + "&from=2026-05-28T00%3A00%3A00%2B00%3A00"
      + "&to=2026-05-29T00%3A00%3A00%2B00%3A00",
    );
  });


  it("listProjectLiveTimeline composes the days param", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ entries: [] }),
    });
    await listProjectLiveTimeline("demo", { days: 7 });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/projects/demo/live/timeline?days=7",
    );
  });


  it("listProjectLiveTimeline omits the days param when not supplied", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({ entries: [] }),
    });
    await listProjectLiveTimeline("demo");
    const [url] = fetchMock.mock.calls[0];
    // No trailing ``?`` when there's nothing to encode — keeps the
    // URL clean and matches the server's default-days path.
    expect(url).toBe(
      "https://api.example.com/v1/projects/demo/live/timeline",
    );
  });


  it("getLiveAggregate threads from / to into the query string", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        row_count: 0, row_pass_count: 0, row_fail_count: 0,
        cost_usd: 0, run_count: 0,
      }),
    });
    await getLiveAggregate("demo", {
      from: "2026-05-28T00:00:00+00:00",
      to:   "2026-05-29T00:00:00+00:00",
    });
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/projects/demo/live/aggregate"
      + "?from=2026-05-28T00%3A00%3A00%2B00%3A00"
      + "&to=2026-05-29T00%3A00%3A00%2B00%3A00",
    );
  });


  it("getLiveAggregate without bounds hits the all-time path", async () => {
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        row_count: 0, row_pass_count: 0, row_fail_count: 0,
        cost_usd: 0, run_count: 0,
      }),
    });
    await getLiveAggregate("demo");
    const [url] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "https://api.example.com/v1/projects/demo/live/aggregate",
    );
  });
});
