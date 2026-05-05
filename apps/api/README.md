# EvalGuard API

FastAPI server that accepts runs from `evalguard push` and serves them
under `/v1/runs/{run_id}` matching the same JSON contract `evalguard
view --json` produces. Phase 2 of the project plan; Apache-2.0
licensed (separate from the MIT-licensed CLI).

## Quickstart

```bash
# 1. Install the server package (workspace member)
uv pip install -e apps/api

# 2. Set a bearer token. Without it, the server runs in "open mode"
#    (loud startup warning + advertised in /v1/health).
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
| `POST` | `/v1/runs`           | Member. Ingest under caller's org. 201 + `Location`; 409 on duplicate `run_id`. |
| `GET`  | `/v1/runs`           | Member. Scoped to caller's org. Query: `project=<slug>&limit=20`. |
| `GET`  | `/v1/runs/{run_id}`  | Member. 404 on cross-org access (no info leak). |
| `GET`  | `/openapi.json`      | OpenAPI 3 spec (auto-generated). |
| `GET`  | `/docs`              | Swagger UI. |

## Configuration

All settings come from the environment. Defaults in parens.

| Var | Default | Notes |
|---|---|---|
| `EVALGUARD_DATABASE_URL`     | `sqlite:///./.evalguard/server.db` | Postgres URL support lands in Phase 2.5. |
| `EVALGUARD_API_KEY`          | *(empty → open mode)* | Single shared bearer for the MVP. Per-org keys later. |
| `EVALGUARD_DEFAULT_ORG`      | `default`                          | Default tenancy auto-provisioned at startup. |
| `EVALGUARD_DEFAULT_PROJECT`  | `default`                          | Used when a run's `project` field doesn't match an existing one (which then auto-upserts). |
| `EVALGUARD_CORS_ORIGINS`     | `*`                                | Comma-separated allowlist, or `*`. |
| `EVALGUARD_HOST`             | `127.0.0.1`                        | Bind host for the `evalguard-api` console script. |
| `EVALGUARD_PORT`             | `8787`                             | Bind port. |

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

What's in 2.5b (next):

- Postgres + Alembic + SQLAlchemy core (DATABASE_URL is the seam;
  the SQLite schema mirrors the eventual Postgres tables).
- Postgres-only RLS policies as defense-in-depth on top of the
  application-layer auth shipped here.

## Persistence shape

The server stores the canonical `run_to_dict()` JSON under
`runs.payload_json` AND denormalizes trials / rows / gates / assets
into joinable tables so the eventual UI doesn't have to parse JSON
to query. Audit events arrive as a JSON blob in `events.events_json`;
per-event indexing is a later phase.

## Idempotency

`POST /v1/runs` with an existing `run_id` returns 409. The reasoning:
re-pushing the same run isn't a merge — runs are immutable artifacts.
Future versions may add `PUT /v1/runs/{run_id}` for explicit
replacement (e.g. re-ingesting a corrected baseline).

## Auth

Single shared bearer token via `EVALGUARD_API_KEY`. Constant-time
compare. Missing-or-malformed headers return 401 with a
`WWW-Authenticate: Bearer` response header. Health endpoint is
unauthenticated so load balancers can probe it.

When `EVALGUARD_API_KEY` is empty, the server runs in **open mode**
(no auth check). This is for local dev only; production deploys
must set the env var. Open mode is loudly advertised:

- A startup log warning.
- `GET /v1/health` returns `"mode": "open"`.

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

- **Next.js UI** — separate package (Phase 2 part 2).
- **Postgres** — the `DATABASE_URL` env var is the seam; the SQLite
  schema mirrors the eventual Postgres tables 1:1.
- **Async workers (Arq + Redis)** — synchronous POST is sufficient
  for the ingest workload; async queues land when long-running tasks
  appear (re-evaluation, drift recomputation, etc.).
- **OIDC / SCIM / RBAC** — single shared bearer for now. The
  `api_keys` table is reserved for per-org keys (Phase 2.5).
- **Hash-chain verification on ingest** — the chain is for at-rest
  tamper detection; HTTPS handles transit. Server-side re-verification
  on read lands when the audit-export endpoint does.
- **Run mutation** — runs are immutable artifacts.
- **`apps/api/ee/`** enterprise modules under ELv2 — Phase 5.

## License

Apache-2.0 (server core). See `LICENSE` at repo root for the full
text. The MIT-licensed CLI (`packages/cli`) and `evalguard-evaluators`
(`packages/evaluators`) remain MIT.
