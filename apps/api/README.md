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
| `POST` | `/v1/runs`           | Auth. Ingest a run. 201 with `Location` header; 409 on duplicate `run_id`. |
| `GET`  | `/v1/runs`           | Auth. List recent runs. Query: `project=<slug>&limit=20`. |
| `GET`  | `/v1/runs/{run_id}`  | Auth. Full run JSON + server-injected `server` envelope. |
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

The schema is multi-tenant from day one (tables: `orgs`, `projects`,
`api_keys`, plus run-shape tables with `project_id` FK). The MVP
operates as a **single tenant** by default — every push goes to the
default org, project resolution comes from the `project: <name>`
field in the run payload (auto-creating projects on first sight).

Phase 2.5 will land:

- Per-org API keys (the `api_keys` table is reserved for this).
- Postgres + Alembic + RLS policies (the schema is structurally
  ready; the change is the driver and migration tooling).
- Org/Project CRUD endpoints.

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
