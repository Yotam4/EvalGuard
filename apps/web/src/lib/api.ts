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


/**
 * Surface the server's clean ``detail`` string when the error is an
 * ``ApiError``, falling back to the plain message for everything else.
 *
 * Round-9 review-pass: every ``useQuery``/``useMutation`` error-
 * rendering site was inlining the same ternary
 * (``e instanceof Error ? e.message : String(e)``).  For an
 * ``ApiError`` that produced the ugly ``"422: name already in use"``
 * concatenated form instead of just ``"name already in use"`` — the
 * same gap the config editor's mutation onError already fixed.
 * Centralising the formatter ensures every error-display site stays
 * consistent and a future tweak (e.g. localised prefixes) flows to
 * every page from one edit.
 */
export function fmtError(e: unknown): string {
  if (e instanceof ApiError) return e.detail;
  if (e instanceof Error)    return e.message;
  return String(e);
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

// PROXY-2 added ``live`` for proxied production calls.  Keeping
// the union open here means the tab strip / source filter can light
// up the new third source as soon as a project receives its first
// proxied call.
export type RunSource = "cli" | "otlp" | "live";


export const listRuns = (
  opts: { project?: string; source?: RunSource; limit?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.project) qs.set("project", opts.project);
  if (opts.source)  qs.set("source",  opts.source);
  if (opts.limit)   qs.set("limit",   String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<RunListResponse>(`/v1/runs${suffix}`);
};


// ---------------------------------------------------------------------------
// Calls stream (Phase OBS).  Mirrors ``CallSummary`` /
// ``CallListResponse`` / ``CallDetail`` in ``apps/api/evalguard_api/models.py``.


// PROXY-2.5 added ``passed`` — the "find golden candidates" surface.
// ``passed`` + ``failures`` partition ``recent``; the UI tab strip
// enforces single-selection so they never combine.
export type CallsTab = "recent" | "failures" | "passed";


export interface CallSummary {
  run_id: string;
  row_id: string;
  trial_id: string;
  project_id: string;
  passed: boolean;
  cost_usd: number;
  latency_ms: number;
  cache_hit: boolean;
  tags: string[];
  ingested_at: string | null;
  output_preview: string | null;
  // PROXY-2.5 review-pass — surfaced by the server's correlated
  // ``runs.source`` subquery so the CallCard can show a "live" /
  // "cli" / "otlp" badge without a second fetch.  ``null`` for
  // legacy rows where the join missed (treated as unknown).
  source: RunSource | null;
}


export interface CallListResponse {
  calls: CallSummary[];
  next_cursor: string | null;
}


export interface CallDetail {
  run_id: string;
  row_id: string;
  trial_id: string | null;
  project_id: string;
  project: string;
  ingested_at: string | null;
  provider: string | null;
  model: string | null;
  passed: boolean;
  n_scores: number;
  cost_usd: number;
  latency_ms: number;
  cache_hit: boolean;
  tags: string[];
  // PROXY-2.5 review-pass: live calls that failed (provider error,
  // timeout, evaluator crash) record their failure reason here so
  // the drill-down panel can explain what went wrong instead of
  // silently showing null output.  ``null`` for batch rows and for
  // successful proxy calls.
  error: string | null;
  // The "actual answer" surface — these can all be null when:
  //  - ``include_scores=False`` push omits them
  //  - cache hits / errors have no output
  //  - dataset has no ``expected``
  input:    unknown;
  expected: unknown;
  output:   string | null;
  scores:   Score[];
  trial_gates: Gate[];
}


export const listProjectCalls = (
  projectSlug: string,
  opts: {
    tab?:    CallsTab;
    cursor?: string;
    limit?:  number;
    source?: RunSource;
    // PROXY-2.5: half-open ``[from, to)`` window on
    // ``ingested_at``.  Both are optional and combine cleanly with
    // ``tab`` / ``source`` / ``cursor``.
    from?:   string;
    to?:     string;
  } = {},
) => {
  const qs = new URLSearchParams();
  qs.set("tab", opts.tab ?? "recent");
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit)  qs.set("limit",  String(opts.limit));
  if (opts.source) qs.set("source", opts.source);
  if (opts.from)   qs.set("from",   opts.from);
  if (opts.to)     qs.set("to",     opts.to);
  return request<CallListResponse>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/calls?${qs.toString()}`,
  );
};


// PROXY-2.5 — live-run timeline + range aggregate.

export interface LiveTimelineEntry {
  run_id:         string;
  started_at:     string | null;
  finished_at:    string | null;
  row_count:      number;
  row_pass_count: number;
  row_fail_count: number;
  cost_usd:       number;
}
export interface LiveTimelineResponse {
  entries: LiveTimelineEntry[];
}

export interface LiveAggregate {
  row_count:      number;
  row_pass_count: number;
  row_fail_count: number;
  cost_usd:       number;
  run_count:      number;
}

export const listProjectLiveTimeline = (
  projectSlug: string, opts: { days?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.days) qs.set("days", String(opts.days));
  const tail = qs.toString();
  return request<LiveTimelineResponse>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/live/timeline`
    + (tail ? `?${tail}` : ""),
  );
};

export const getLiveAggregate = (
  projectSlug: string, opts: { from?: string; to?: string } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.from) qs.set("from", opts.from);
  if (opts.to)   qs.set("to",   opts.to);
  const tail = qs.toString();
  return request<LiveAggregate>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/live/aggregate`
    + (tail ? `?${tail}` : ""),
  );
};


export const getCallDetail = (
  projectSlug: string, runId: string, rowId: string,
  opts: { trialId?: string | null } = {},
) => {
  // ``trial_id`` disambiguates a row_id shared across trials in a
  // multi-trial comparison run — without it the server returns the
  // first trial's answer for that row.
  const qs = opts.trialId
    ? `?trial_id=${encodeURIComponent(opts.trialId)}`
    : "";
  return request<CallDetail>(
    `/v1/projects/${encodeURIComponent(projectSlug)}`
    + `/calls/${encodeURIComponent(runId)}`
    + `/${encodeURIComponent(rowId)}${qs}`,
  );
};


// ---------------------------------------------------------------------------
// Golden candidates (Phase OBS-4).  Mirrors ``GoldenCandidate*``
// in ``apps/api/evalguard_api/models.py``.


/** The promoted row's content — present only when the list is
 *  fetched with ``expand: "row"``; null otherwise or when the
 *  parent run was deleted. */
export interface GoldenRowData {
  input?: unknown;
  expected?: unknown;
  output?: string | null;
}


export interface GoldenCandidate {
  id: number;
  run_id: string;
  row_id: string;
  project_id: string;
  promoted_by: string;
  note: string | null;
  created_at: string;
  row_data?: GoldenRowData | null;
}


export const promoteToGolden = (body: {
  run_id: string;
  row_id: string;
  note?: string;
}) =>
  request<GoldenCandidate>(`/v1/golden/candidates`, {
    method: "POST",
    body: JSON.stringify(body),
  });


export const listGoldenCandidates = (
  projectSlug: string,
  opts: { limit?: number; expand?: "row" } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.expand) qs.set("expand", opts.expand);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<{ candidates: GoldenCandidate[] }>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/golden/candidates${suffix}`,
  );
};


export const unPromoteGolden = (id: number) =>
  request<void>(`/v1/golden/candidates/${id}`, {
    method: "DELETE",
  });


// PROXY-4 — project_configs read + write.
//
// Wire-shape pinned in ``apps/api/evalguard_api/models.py``:
// - ``ProjectConfig`` carries the full record incl. raw ``content``
// - ``ProjectConfigSummary`` omits content (used in the history list)
// - ``ProjectConfigHistory`` is just ``{configs: ProjectConfigSummary[]}``
// The server validates the YAML shape at push time (round-4 ticket #4),
// so a 422 carries a human-readable ``detail`` string the editor can
// surface inline.

export interface ProjectConfig {
  id:             number;
  project_id:     string;
  content_sha256: string;
  content:        string;
  pushed_by:      string;
  pushed_at:      string;
}

export interface ProjectConfigSummary {
  id:             number;
  project_id:     string;
  content_sha256: string;
  pushed_by:      string;
  pushed_at:      string;
}

export interface ProjectConfigHistory {
  configs: ProjectConfigSummary[];
}

export const getLatestProjectConfig = (projectSlug: string) =>
  request<ProjectConfig>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/config`,
  );

export const getProjectConfigHistory = (
  projectSlug: string, opts: { limit?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.limit) qs.set("limit", String(opts.limit));
  const tail = qs.toString();
  return request<ProjectConfigHistory>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/config/history`
    + (tail ? `?${tail}` : ""),
  );
};

export const getProjectConfigRevision = (
  projectSlug: string, configId: number,
) =>
  request<ProjectConfig>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/config/${configId}`,
  );

export const pushProjectConfig = (
  projectSlug: string, content: string,
) =>
  // Server returns ``201`` for a new revision and ``200`` when the
  // SHA-256 matches an existing one (idempotent push of the same
  // bytes).  ``request<T>`` unwraps the body in both cases; the
  // caller can compare ``pushed_at`` against the previous fetch
  // to tell whether their push was a no-op.
  request<ProjectConfig>(
    `/v1/projects/${encodeURIComponent(projectSlug)}/config`,
    {
      method: "POST",
      body:   JSON.stringify({ content }),
    },
  );


export const getRun = (runId: string) =>
  request<RunOut>(`/v1/runs/${encodeURIComponent(runId)}`);

// ---------------------------------------------------------------------------
// Drift — Welch's t-test across two runs (Phase 3b).
// Mirrors ``DriftReport`` / ``DriftMetric`` in ``models.py``.

export interface DriftMetric {
  name: "latency_ms" | "cost_usd" | "passed";
  n_current: number;
  n_baseline: number;
  mean_current: number;
  mean_baseline: number;
  delta_mean: number;
  t_stat: number;
  dof: number;
  p_two_sided: number;
  /** One-sided p-value for "current < baseline". Small ⇒ regression on metrics where lower is worse. */
  p_less: number;
  /** One-sided p-value for "current > baseline". Small ⇒ regression on cost / latency. */
  p_greater: number;
  significant_at_alpha: boolean;
}

export interface DriftSkip {
  name: string;
  reason: string;
}

export interface DriftReport {
  current_run_id: string;
  baseline_run_id: string;
  alpha: number;
  metrics: DriftMetric[];
  skipped: DriftSkip[];
}

export const getRunDrift = (
  runId: string,
  baselineId: string,
  opts: { alpha?: number } = {},
) => {
  const qs = new URLSearchParams({ vs: baselineId });
  if (opts.alpha != null) qs.set("alpha", String(opts.alpha));
  return request<DriftReport>(
    `/v1/runs/${encodeURIComponent(runId)}/drift?${qs.toString()}`,
  );
};

// ---------------------------------------------------------------------------
// Reviews — Phase 4 human review queue.
// Mirrors ``ReviewIngest`` / ``ReviewOut`` / ``ReviewQueueItem`` in
// ``apps/api/evalguard_api/models.py``.

export type ReviewVerdict = "agree" | "override_pass" | "override_fail" | "skip";

export interface ReviewQueueItem {
  run_id: string;
  row_id: string;
  trial_id: string;
  project_id: string;
  passed: boolean;
  cost_usd: number;
  latency_ms: number;
  tags: string[];
  failing_gates: string[];
}

export interface ReviewQueueResponse {
  items: ReviewQueueItem[];
  run_id: string | null;
}

export interface Review {
  id: number;
  run_id: string;
  row_id: string;
  project_id: string;
  reviewer_key_id: string;
  verdict: ReviewVerdict;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export const getReviewQueue = (runId: string, opts: { limit?: number } = {}) => {
  const qs = new URLSearchParams({ run_id: runId });
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  return request<ReviewQueueResponse>(`/v1/reviews/queue?${qs.toString()}`);
};

export const submitReview = (body: {
  run_id: string;
  row_id: string;
  verdict: ReviewVerdict;
  note?: string;
}) =>
  request<Review>(`/v1/reviews`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const listRunReviews = (runId: string) =>
  request<{ reviews: Review[] }>(
    `/v1/runs/${encodeURIComponent(runId)}/reviews`,
  );

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


// Phase 2.6d — drill-down for one asset.
export interface AssetVersionRecord {
  version_id:   string;
  run_id:       string;
  project_name: string;
  ingested_at:  string;
  source:       "cli" | "otlp";
}

export interface AssetVersionsResponse {
  kind:         AssetKind;
  asset_id:     string;
  project_id:   string;
  project_name: string;
  versions:     AssetVersionRecord[];
}

export const listAssetVersions = (
  kind: AssetKind, assetId: string, projectId: string,
  opts: { limit?: number } = {},
) => {
  const qs = new URLSearchParams({ project_id: projectId });
  if (opts.limit) qs.set("limit", String(opts.limit));
  // Path segments must be percent-encoded — an asset_id with a
  // ``/`` would otherwise look like a sub-resource to the router.
  return request<AssetVersionsResponse>(
    `/v1/assets/${encodeURIComponent(kind)}`
    + `/${encodeURIComponent(assetId)}`
    + `/versions?${qs.toString()}`,
  );
};
