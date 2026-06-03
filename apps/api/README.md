# EvalGuard API

FastAPI server that accepts runs from `evalguard push` and serves them
under `/v1/runs/{run_id}` matching the same JSON contract `evalguard
view --json` produces. Phase 2 of the project plan; Apache-2.0
licensed (separate from the MIT-licensed CLI).

## Quickstart

```bash
# 1. Install the server package (workspace member)
uv pip install -e apps/api

# 2. Set a bearer token.  Without one the server refuses to boot
#    unless ``EVALGUARD_OPEN_MODE=1`` is set AND the bind host is
#    loopback — open mode is dev-only by construction.
export EVALGUARD_API_KEY="$(openssl rand -hex 32)"

# 3. Run with uvicorn
uvicorn evalguard_api.main:app --host 0.0.0.0 --port 8787

# 4. From another shell, run an eval and push it
cd examples/quickstart-summarizer
evalguard run
EVALGUARD_SERVER=http://localhost:8787 \
EVALGUARD_API_TOKEN=$EVALGUARD_API_KEY \
  evalguard push --last
# → pushed run_<id> → http://localhost:8787/v1/runs (201)

# 5. Read it back
curl -s -H "Authorization: Bearer $EVALGUARD_API_KEY" \
     http://localhost:8787/v1/runs/run_<id> | jq
```

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET`  | `/v1/health`         | No auth. Returns `{status, version, mode, db}`. |
| `POST` | `/v1/orgs`           | Admin. Create a new tenant. 409 on duplicate slug. |
| `GET`  | `/v1/orgs`           | Auth. List visible orgs (admin: all; member: own). |
| `GET`  | `/v1/orgs/{org_id}`  | Member of org or admin. |
| `POST` | `/v1/orgs/{org_id}/api_keys` | Member of org or admin. Returns plaintext token **once** + summary. |
| `GET`  | `/v1/orgs/{org_id}/api_keys` | Member of org or admin. Listing never includes plaintext / hashed values. |
| `DELETE` | `/v1/api_keys/{key_id}` | Member of key's org or admin. Idempotent. |
| `POST` | `/v1/projects`       | Member. `?org_id=` admin-only override. 409 on duplicate slug *within same org*. |
| `GET`  | `/v1/projects`       | Member. Scoped to caller's org by default. |
| `GET`  | `/v1/projects/{slug}` | Member. 404 (not 403) on cross-org probe. |
| `POST` | `/v1/runs`           | Member. Ingest under caller's org. 201 + `Location`; 409 on duplicate `run_id` (or 200 + `idempotent_replay` with an `Idempotency-Key`). |
| `GET`  | `/v1/runs`           | Member. Scoped to caller's org. Query: `project=<slug-or-name>&source=cli\|otlp\|live&limit=20`. `project` matches either the display name or slug — both inter-page links and a manually-typed filter converge on the same query. |
| `GET`  | `/v1/runs/{run_id}`  | Member. 404 on cross-org access (no info leak). |
| `GET`  | `/v1/runs/{run_id}/drift` | Member. Welch's t-test vs another run. Query: `vs=<run_id>&alpha=0.05`. 400 if `run_id==vs`; 404 if either is missing/cross-org. |
| `GET`  | `/v1/runs/{run_id}/reviews` | Member. Every reviewer's verdict on the run. |
| `POST` | `/v1/otlp/v1/traces` | Member. OTLP/HTTP JSON ingest of GenAI spans → synthetic runs (`source=otlp`). Head-sampled by `EVALGUARD_OTLP_SAMPLE_RATE`. Returns `{partialSuccess:{}, evalguard:{accepted_runs, kept_spans, dropped_spans}}`. |
| `GET`  | `/v1/assets`         | Member. Cross-run aggregation by `(kind, asset_id)`. Query: `kind=prompt\|dataset\|judge\|heuristic\|metric\|schema\|rubric&project=<slug>&limit=100`. |
| `GET`  | `/v1/assets/{kind}/{asset_id}/versions` | Member. Every `(version_id, run_id, ingested_at)` for one asset. Query: `project_id=<id>` (required) `&limit=200`. 400 unknown kind; 404 missing/cross-org. |
| `GET`  | `/v1/reviews/queue`  | Member. Rows of a run that failed + the caller hasn't reviewed. Query: `run_id=<id>&limit=50`. |
| `POST` | `/v1/reviews`        | Member. Submit `{run_id, row_id, verdict, note?}`; verdict ∈ `agree\|override_pass\|override_fail\|skip`. UPSERT per reviewer. |
| `GET`  | `/v1/projects/{slug}/calls` | Member. Cursor-paginated call stream. Query: `tab=recent\|failures\|passed&cursor=&limit=50&source=cli\|otlp\|live&from=&to=`. `from`/`to` is half-open `[from, to)` on `ingested_at` (PROXY-2.5). |
| `GET`  | `/v1/projects/{slug}/live/timeline` | Member. **PROXY-2.5.** Daily live runs newest-first, one entry per `run_live_*`. Query: `days=30` (max 90). |
| `GET`  | `/v1/projects/{slug}/live/aggregate` | Member. **PROXY-2.5.** SUM of pass / fail / cost / run count across `[from, to)`. Omit bounds for all-time. |
| `GET`  | `/v1/projects/{slug}/calls/{run_id}/{row_id}` | Member. One call's full input/expected/output/scores/gates. |
| `POST` | `/v1/golden/candidates` | Member. Promote `{run_id, row_id, note?}` to the golden staging table. Idempotent per reviewer. |
| `GET`  | `/v1/projects/{slug}/golden/candidates` | Member. Staged candidates. Query: `limit=100&expand=row` (`expand=row` attaches input/expected/output). |
| `DELETE` | `/v1/golden/candidates/{id}` | Original promoter or admin. 403 for others; 404 unknown. |
| `POST` | `/v1/projects/{slug}/config` | Member. Upload an `evalguard.yaml` blob. Content-addressed by SHA-256 → 200 (idempotent) if bytes already stored; 201 for a new revision. |
| `GET`  | `/v1/projects/{slug}/config` | Member. Latest config revision for the project. 404 if no config has been pushed yet. |
| `GET`  | `/v1/projects/{slug}/config/history` | Member. Recent revisions newest-first (content omitted). Query: `limit=20`. |
| `GET`  | `/v1/projects/{slug}/config/{config_id}` | Member. Specific revision verbatim — the pin-to-hash surface the proxy invoke path uses. |
| `POST` | `/v1/projects/{slug}/invoke` | Member. **Phase PROXY-2.** Forward one LLM call through the project's stored provider, score against the configured evaluators, record under the day's live run (`source='live'`), return the model's output. 502 on provider failure (row still recorded). 422 if no config / no providers. 429 on per-key rate-limit exhaustion; 402 on per-project daily cost-cap exhaustion. |
| `GET`  | `/v1/projects/{slug}/audit/events` | Member. **Phase PROXY-3.5.** List events emitted by a live run, in chain order. Query: `run_id=<id>&limit=500`. Response carries `corrupt_rows` + `truncated` so a partial chain has a clear signal. |
| `GET`  | `/v1/projects/{slug}/audit/verify` | Member. **Phase PROXY-3.5.** Walk the per-run hash chain and verify every `prev_event_hash → event_hash` link. Returns `{ok, events, broken_at, reason}` matching the CLI's `verify_chain` shape. 413 if the chain exceeds the verify page cap (cursor pagination is on the roadmap; refuses rather than reporting a misleading partial-chain `ok=true`). |
| `GET`  | `/openapi.json`      | OpenAPI 3 spec (auto-generated). |
| `GET`  | `/docs`              | Swagger UI. |

The endpoint table is the human summary; `/openapi.json` is the
authoritative generated contract. Feature-level docs:
[`docs/observability.md`](../../docs/observability.md) (calls / OTLP /
drift) and [`docs/golden-dataset.md`](../../docs/golden-dataset.md)
(promote + export).

## Configuration

All settings come from the environment. Defaults in parens.

| Var | Default | Notes |
|---|---|---|
| `EVALGUARD_DATABASE_URL`     | `sqlite:///./.evalguard/server.db` | SQLite or `postgresql+psycopg://…` (install `apps/api[postgres]`). |
| `EVALGUARD_API_KEY`          | *(empty)*                          | Bootstrap admin token. Materialized as an `admin` API key in the default org on first startup. **If empty**, server refuses to boot unless `EVALGUARD_OPEN_MODE=1` is set AND the bind host is loopback. |
| `EVALGUARD_OPEN_MODE`        | `0`                                | Set `1` to explicitly opt into the no-auth dev posture.  Refused when bind host isn't loopback or `EVALGUARD_CORS_ORIGINS=*`. |
| `EVALGUARD_DEFAULT_ORG`      | `default`                          | Default tenancy auto-provisioned at startup. |
| `EVALGUARD_DEFAULT_PROJECT`  | `default`                          | Used when a run's `project` field doesn't match an existing one (which then auto-upserts). |
| `EVALGUARD_CORS_ORIGINS`     | *(empty — no cross-origin)*        | Comma-separated allowlist (web app origin), or `*` for dev. `*` combined with auth mode is fine; `*` combined with open mode is refused. |
| `EVALGUARD_HOST`             | `127.0.0.1`                        | Bind host for the `evalguard-api` console script. Open mode requires this to be loopback. |
| `EVALGUARD_PORT`             | `8787`                             | Bind port. |
| `EVALGUARD_TRUSTED_HOSTS`    | *(empty — accept all)*             | Comma-separated `Host:` header allowlist. Use behind a reverse proxy in prod. |
| `EVALGUARD_REQUIRE_HTTPS`    | `0`                                | Set `1` to redirect plain-HTTP requests to HTTPS (behind a TLS-terminating proxy). |
| `EVALGUARD_MAX_REQUEST_BYTES`| `1048576` (1 MiB)                  | Hard cap on request body size; oversize → 413. |
| `EVALGUARD_OTLP_SAMPLE_RATE` | `1.0`                              | Head-based deterministic sampling on OTLP ingest. `0.0`–`1.0`. |
| `EVALGUARD_DB_POOL_SIZE`     | `5`                                | SQLAlchemy pool size (Postgres only; SQLite stays single-connection). |
| `EVALGUARD_DB_MAX_OVERFLOW`  | `10`                               | Pool overflow ceiling. |
| `EVALGUARD_DB_POOL_PRE_PING` | `1`                                | Enable `SELECT 1` health check before checkout (recommended). |
| `EVALGUARD_DB_POOL_RECYCLE_S`| `1800`                             | Recycle connections older than N seconds. |

## Multi-tenancy model

Three-level hierarchy: **Org → Project → Run**. Every authenticated
caller resolves to a `Principal(org_id, key_id, scopes)`. The
`api_keys` table is the source of truth: each token's sha256 hash
sits in a row; the row's `org_id` is the caller's tenant; the row's
`scopes_csv` decides whether they can act cross-org (`admin`) or
only on their own org (empty).

**Token shape:** server-minted tokens are `evk_<32 hex>` — the
`evk_` prefix is searchable by secret-scanners (GitHub, gitleaks,
trufflehog) so leaks get caught quickly. The `EVALGUARD_API_KEY`
env-bootstrap token is materialized as an `admin` key in the
default org on first startup; it can be any string the operator
chose (so existing single-tenant deployments don't have to rotate).

**Cross-tenant guarantees:**

- `GET /v1/runs/{run_id}` returns 404 (not 403) when the run is in
  another org. Same response as "id never existed" — no enumeration
  leak.
- `GET /v1/runs` and `GET /v1/projects` listings are silently
  scoped: members see only their own org's rows. Per-row 403 would
  leak existence.
- `GET /v1/orgs` does the same: members see one entry (their own).
- `POST /v1/projects?org_id=other-org` is **403** for non-admin
  callers explicitly targeting a foreign org — that path can't
  produce ambiguity.

**Project ids** are random opaque (`proj_<random16>`); the
`(org_id, slug)` composite is the uniqueness boundary. Two orgs may
both have a `demo` project without colliding.

## Postgres deployment (Phase 2.5b)

Install with the `postgres` extra and point `EVALGUARD_DATABASE_URL`
at your cluster:

```bash
uv pip install -e 'apps/api[postgres]'
export EVALGUARD_DATABASE_URL=postgresql+psycopg://evalguard:secret@db:5432/evalguard
export EVALGUARD_API_KEY=$(openssl rand -hex 32)
uvicorn evalguard_api.main:app --host 0.0.0.0 --port 8787
```

The lifespan applies all pending Alembic migrations on every
startup (idempotent; tracked in `alembic_version`). Bring an empty
database — schema, default tenancy, and the bootstrap admin key
are all materialized on first boot.

### Migrations

| Action | Command |
|---|---|
| Apply pending migrations | runtime auto-applies on startup; or manually: `cd apps/api && alembic upgrade head` |
| Generate a new migration from a `metadata` change | `cd apps/api && alembic revision --autogenerate -m "<message>"` |
| Roll back one revision | `cd apps/api && alembic downgrade -1` |

Migration scripts live in `apps/api/evalguard_api/migrations/versions/`.
`0002_rls_policies.py` is **Postgres-only** (no-ops on SQLite via an
`op.get_bind().dialect.name` check).

### Row-Level Security

When the server runs against Postgres, `0002_rls_policies` enables
RLS on every table that carries tenant data (`projects`, `api_keys`,
`runs`, `trials`, `run_rows`, `gate_results`, `assets`, `events`)
and creates `*_tenant_isolation` policies that enforce visibility
based on two session-local GUCs set per-transaction in `deps.get_conn`:

- `app.org_id` — caller's `Principal.org_id`
- `app.is_admin` — `"1"` for admin scope, `"0"` otherwise

RLS enforces the same contract as the application-layer guards in
`auth.py` — at the database level, so a query that forgets a
`WHERE org_id = …` filter (or a future code path that bypasses the
route guards) still can't leak across tenants. Admin scope bypasses
the policies (intentional — admin keys are the only way to inspect
cross-org state).

### Testing against Postgres

The default test suite runs on SQLite. The Postgres integration
suite at `tests/api/test_postgres.py` is gated by
`EVALGUARD_TEST_POSTGRES_URL`:

```bash
docker run --rm -d --name eg-test-pg -p 5433:5432 \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=eg_test \
  postgres:16-alpine

export EVALGUARD_TEST_POSTGRES_URL=postgresql+psycopg://test:test@localhost:5433/eg_test
pytest tests/api/test_postgres.py
```

These tests verify Alembic applies cleanly, RLS is actually
enabled (queries `pg_class.relrowsecurity`), and cross-tenant
access is blocked at the DB layer in addition to the app layer.

## Persistence shape

The server stores the canonical `run_to_dict()` JSON under
`runs.payload_json` AND denormalizes trials / rows / gates / assets
into joinable tables so the UI doesn't have to parse JSON to query.

Batch ingest still writes the full audit log as a JSON blob under
`events.events_json` (the CLI's append-only shape).  **The proxy
gateway writes per-event rows** to the `event_rows` table (one row
per `/v1/projects/{slug}/invoke` call), enforcing the chain
invariant at the DB layer via `UNIQUE (run_id, prev_event_hash)`
+ a partial unique index `(run_id) WHERE prev_event_hash IS NULL`
so a concurrent first-of-day insert can't fork the chain root.
`/v1/projects/{slug}/audit/events` + `/audit/verify` walk this
table; `verify_chain_events` (`evalguard_evaluators.audit`) does
the per-event hash check.

## Idempotency

`POST /v1/runs` with an `Idempotency-Key` header returns the
existing run (200 with `idempotent_replay: true` envelope) when the
combination of caller + key + payload matches a previous request,
so a CI retry after a network blip doesn't 409.  Without the header
a duplicate `run_id` returns 409 — runs are immutable artifacts.

The proxy's `/v1/projects/{slug}/invoke` is implicitly idempotent
at the chain layer: a duplicate insert loses the
`UNIQUE (run_id, prev_event_hash)` race and the bounded retry loop
re-reads the chain tip up to 8 times before raising.

## Auth

Bearer token via `EVALGUARD_API_KEY` is the bootstrap admin —
materialised at startup as an `admin` API key in the default org.
Constant-time compare on every request; missing-or-malformed
headers return 401 with a `WWW-Authenticate: Bearer` response
header.  Health endpoint is unauthenticated so load balancers can
probe it.

When `EVALGUARD_API_KEY` is empty, the server **refuses to boot**
unless the operator also sets `EVALGUARD_OPEN_MODE=1` AND the bind
host is loopback (`127.0.0.1` or `::1`).  Open mode + `*` CORS is
refused too — the combo is too easy to deploy to prod by mistake.
Open mode is loudly advertised:

- Startup log warning.
- `GET /v1/health` returns `"mode": "open"`.

## Gateway use (Phase PROXY)

The server is also a record-as-you-go LLM gateway.  Point your
app's OpenAI base URL at `POST /v1/projects/{slug}/invoke` and the
proxy:

1. Loads the project's latest pushed `evalguard.yaml` (`/v1/projects/{slug}/config`).
2. Fires the request at the configured provider with the same SDK
   pass-through every other path uses.
3. Scores the response against the project's evaluators (heuristics
   / metrics / judges).
4. Records the call as one row of the day's lazy-created live run
   (`run_id = run_live_<sha256(project_id|date)[:16]>`,
   `source = 'live'`), one event of the per-run audit chain
   (`event_rows`), and one increment of the cost meter.
5. Returns the model's output.

A failing provider call still records (`provider.failed` event,
empty output) and bubbles up as 502.  Rate-limit exhaustion
(`429`) and cost-cap exhaustion (`402`) are both recorded as
audit events so the operator can reconstruct why a particular
minute lost traffic.

### Quotas

`quotas.py` enforces two limits, both configurable per-project in
the pushed YAML:

| YAML key | Default | Behaviour |
|---|---|---|
| `rate_limit_per_minute` | unbounded | Sliding-window per API key; `429` on exhaustion (`Retry-After` header carries the seconds-to-reset). |
| `cost_cap_usd_daily`    | unbounded | Per-project rolling 24h cost; `402` on exhaustion. |

```yaml
# evalguard.yaml fragment a tenant pushes via /v1/projects/{slug}/config
rate_limit_per_minute: 600
cost_cap_usd_daily: 50.00
```

## Operations

### Docker

```bash
docker build -t evalguard-api -f apps/api/Dockerfile .
docker run --rm -p 8787:8787 \
  -e EVALGUARD_API_KEY=$KEY \
  -v $PWD/server-data:/data \
  -e EVALGUARD_DATABASE_URL=sqlite:////data/server.db \
  evalguard-api
```

### Health-check

```bash
curl -fsS http://localhost:8787/v1/health
```

Use as a Kubernetes liveness/readiness probe. Returns 200 with a
small JSON body once the lifespan completes (schema initialized,
default tenancy provisioned).

## What's NOT in scope (yet)

- **Async workers (Arq + Redis)** — synchronous POST is sufficient
  for the ingest workload; async queues land when long-running tasks
  appear (re-evaluation, drift recomputation, etc.).
- **OIDC / SAML / SCIM** — per-org API keys + scopes cover the
  shipped multi-tenant story.  Federated identity lives on the
  enterprise tier.
- **Hash-chain verification on ingest** — the chain is for at-rest
  tamper detection; HTTPS handles transit.  Server-side
  re-verification on read is available via `/audit/verify`.
- **Cursor pagination on `/audit/verify`** — chains beyond the
  500-event verify cap return 413; the verify path refuses rather
  than reporting `ok=true` for a visible prefix.
- **Run mutation** — runs are immutable artifacts.
- **`apps/api/ee/`** enterprise modules under ELv2 — Phase 5.

## License

Apache-2.0 (server core). See `LICENSE` at repo root for the full
text. The MIT-licensed CLI (`packages/cli`) and `evalguard-evaluators`
(`packages/evaluators`) remain MIT.
