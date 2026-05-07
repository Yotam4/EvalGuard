/**
 * Typed client for the EvalGuard FastAPI server.
 *
 * Types mirror ``packages/schemas/evalguard.run.schema.json`` and
 * the ``apps/api/evalguard_api/models.py`` Pydantic shapes. The
 * server enforces both ends of the contract; this file is the
 * browser-side mirror so a column rename surfaces as a TypeScript
 * error rather than a runtime ``undefined``.
 *
 * Auth + base URL come from ``localStorage`` (managed by the
 * Settings page in ``src/app/settings/page.tsx``) so a single
 * static bundle can deploy across staging/prod and the operator
 * picks where it points at runtime.
 */

import { getServerUrl, getToken } from "./auth";

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

export class NotConfiguredError extends Error {
  constructor() {
    super("Server URL or API token is not configured. Open Settings.");
  }
}

// Explicit allowlist of paths the UI should call WITHOUT a bearer
// token. Using ``endsWith("/health")`` (the previous shape) would
// also unauthenticate any future endpoint coincidentally ending in
// "/health", e.g. ``/v1/runs/.../health-check`` — silent regression.
// Keep this set in lockstep with the server's ``main.py`` allowlist.
const PUBLIC_PATHS: ReadonlySet<string> = new Set(["/v1/health"]);

// Default per-request timeout. A hung server (or bad network) without
// this hangs the UI's mutation/spinner indefinitely. 30s is generous
// for the listing/detail endpoints; long-running mutations should
// pass an explicit ``signal`` from a route-level AbortController.
const DEFAULT_TIMEOUT_MS = 30_000;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = getServerUrl();
  const token = getToken();
  if (!base) throw new NotConfiguredError();

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (token && !PUBLIC_PATHS.has(path.split("?")[0])) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  // Compose caller-supplied AbortSignal with our default timeout so a
  // route can still cancel manually, AND a stuck connection eventually
  // unblocks the UI.
  const timeoutCtl = new AbortController();
  const timer = setTimeout(() => timeoutCtl.abort(), DEFAULT_TIMEOUT_MS);
  const signal = init?.signal ?? timeoutCtl.signal;
  if (init?.signal) {
    init.signal.addEventListener("abort", () => timeoutCtl.abort(), { once: true });
  }

  let res: Response;
  try {
    res = await fetch(`${base.replace(/\/$/, "")}${path}`, {
      ...init,
      headers,
      signal,
      cache: "no-store",
    });
  } catch (err) {
    if (timeoutCtl.signal.aborted) {
      throw new ApiError(0, `request timed out after ${DEFAULT_TIMEOUT_MS / 1000}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // not JSON — keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  // 204 No Content — DELETE responses
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Health


export interface Health {
  status: "ok";
  version: string;
  mode: "auth" | "open";
  db: string;
}

export const health = () => request<Health>("/v1/health");

// ---------------------------------------------------------------------------
// Runs — types mirror evalguard.run.schema.json + RunOut.

export interface RunSummary {
  run_id: string;
  project: string;
  status: string | null;
  gate_status: string | null;
  started_at: string | null;
  finished_at: string | null;
  row_count: number;
  row_pass_count: number;
  row_fail_count: number;
  cost_usd: number;
  ingested_at: string | null;
  ingested_by: string | null;
  /** Phase 3a: which ingest path produced this row. */
  source: "cli" | "otlp";
}

export interface RunListResponse {
  runs: RunSummary[];
  next: string | null;
}

export interface Asset {
  kind: "prompt" | "dataset" | "schema" | "rubric" | "judge" | "heuristic" | "metric";
  asset_id: string;
  version_id: string;
  source: string | null;
}

export interface Score {
  evaluator_id: string;
  evaluator_kind: string;
  layer: number;
  value: number;
  passed: boolean;
  raw?: unknown;
}

export interface Row {
  row_id: string;
  trial_id?: string | null;
  passed: boolean;
  n_scores: number;
  provider?: string | null;
  model?: string | null;
  cost_usd: number;
  latency_ms: number;
  cache_hit: boolean;
  tags: string[];
  input?: unknown;
  expected?: unknown;
  output?: string | null;
  scores?: Score[];
}

export interface GateDetail {
  metric?: string;
  op?: string;
  target?: number;
  actual?: number;
  passed?: boolean;
  // gate detail shapes are polymorphic — leave open.
  [k: string]: unknown;
}

export interface Gate {
  gate_name: string;
  severity: "block" | "warn" | "log";
  blocking?: boolean | null;
  passed: boolean;
  layer?: number | null;
  details: GateDetail[];
}

export interface Trial {
  trial_id: string;
  provider_id: string;
  provider: string;
  model: string;
  prompt_id?: string | null;
  prompt_version_id?: string | null;
  config: Record<string, unknown>;
  row_count: number;
  row_pass_count: number;
  row_fail_count: number;
  cost_usd: number;
  status: string | null;
  gate_status: string | null;
  started_at: string | null;
  finished_at: string | null;
  metrics: Record<string, unknown>;
  gates: Gate[];
  rows: Row[];
}

export interface RunOut {
  schema_version: string;
  run_id: string;
  project: string;
  config_hash?: string | null;
  status?: string | null;
  row_status?: string | null;
  gate_status?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  row_count?: number;
  row_pass_count?: number;
  row_fail_count?: number;
  cost_usd?: number;
  assets?: Asset[];
  trials: Trial[];
  aggregate?: { metrics: Record<string, unknown>; gates: Gate[] };
  // Server-injected envelope (only present on GET responses).
  server?: {
    ingested_at: string;
    ingested_by: string;
    project_id: string;
  };
}

export const listRuns = (opts: { project?: string; limit?: number } = {}) => {
  const qs = new URLSearchParams();
  if (opts.project) qs.set("project", opts.project);
  if (opts.limit) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<RunListResponse>(`/v1/runs${suffix}`);
};

export const getRun = (runId: string) =>
  request<RunOut>(`/v1/runs/${encodeURIComponent(runId)}`);

// ---------------------------------------------------------------------------
// Orgs


export interface Org {
  org_id: string;
  slug: string;
  name: string;
  created_at: string;
}

export const listOrgs = () => request<{ orgs: Org[] }>(`/v1/orgs`);

export const createOrg = (body: { slug: string; name: string }) =>
  request<Org>(`/v1/orgs`, { method: "POST", body: JSON.stringify(body) });

// ---------------------------------------------------------------------------
// Projects


export interface Project {
  project_id: string;
  org_id: string;
  slug: string;
  name: string;
  created_at: string;
}

export const listProjects = (opts: { org_id?: string } = {}) => {
  const qs = opts.org_id
    ? `?org_id=${encodeURIComponent(opts.org_id)}`
    : "";
  return request<{ projects: Project[] }>(`/v1/projects${qs}`);
};

export const createProject = (body: { slug: string; name?: string; org_id?: string }) => {
  const { org_id, ...rest } = body;
  const qs = org_id ? `?org_id=${encodeURIComponent(org_id)}` : "";
  return request<Project>(`/v1/projects${qs}`, {
    method: "POST",
    body: JSON.stringify(rest),
  });
};

// ---------------------------------------------------------------------------
// API keys


export interface ApiKeySummary {
  key_id: string;
  org_id: string;
  prefix: string;
  name: string;
  scopes: string[];
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface ApiKeyCreated {
  key: ApiKeySummary;
  /** Plaintext bearer — returned exactly once. The server never reveals it again. */
  token: string;
}

export const listApiKeys = (orgId: string) =>
  request<{ keys: ApiKeySummary[] }>(
    `/v1/orgs/${encodeURIComponent(orgId)}/api_keys`,
  );

export const createApiKey = (
  orgId: string,
  body: { name: string; scopes?: string[] },
) =>
  request<ApiKeyCreated>(
    `/v1/orgs/${encodeURIComponent(orgId)}/api_keys`,
    { method: "POST", body: JSON.stringify({ scopes: [], ...body }) },
  );

export const revokeApiKey = (keyId: string) =>
  request<void>(`/v1/api_keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
  });


// ---------------------------------------------------------------------------
// Assets — aggregated cross-run view (Phase 2.6c)


export type AssetKind =
  | "prompt" | "dataset" | "schema" | "rubric"
  | "judge"  | "heuristic" | "metric";


export interface AssetSummary {
  kind:            AssetKind;
  asset_id:        string;
  project_id:      string;
  project_name:    string;
  version_count:   number;
  run_count:       number;
  last_seen:       string;
  last_run_id:     string;
  last_version_id: string;
}


export const listAssets = (
  opts: { kind?: AssetKind; project?: string; limit?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.kind)    qs.set("kind",    opts.kind);
  if (opts.project) qs.set("project", opts.project);
  if (opts.limit)   qs.set("limit",   String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<{ assets: AssetSummary[] }>(`/v1/assets${suffix}`);
};
