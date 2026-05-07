# EvalGuard

A controllable environment for evaluating LLM systems.

EvalGuard is **not** a turnkey eval framework with opinions about what
"good" means for your product. It is a **control plane**: a structured,
versioned, gated pipeline you tailor to your own product, your own
golden set, your own rubric, your own thresholds.

```
       golden set                 ┌─ heuristics ──┐
            │                     │ Layer 1       │ block — exit 2 if fail
       ┌────┴────┐                ├─ metrics ─────┤ warn  — exit 0 + annotation
       │ providers│  →  outputs → │ Layer 2       │
       │ prompts  │               ├─ judge offline┤ each gate is independently
       └─────────┘                │ Layer 3       │ controllable: severity,
            ↑                     ├─ judge online │ aggregation, threshold,
       env-substituted            │ Layer 4       │ per-tag overrides,
       configurable               ├─ human        │ custom Python check.
                                  │ Layer 5       │
                                  └───────────────┘
```

## Principles

1. **Configurability is the product.** Temperature, API keys, base URLs,
   models, prompts, judge rubrics, gate thresholds, severity levels, and
   short-circuit policy are all set in `evalguard.yaml`. You don't fork
   the framework to change behavior — you edit your config.
2. **Each pyramid step is its own control panel.** Every gate has its
   own severity (`block` / `warn` / `log`), aggregation (`pass_rate`,
   `mean`, `pass_rate_by_tag`, …), and threshold. Inspect, configure,
   and reason about each step in isolation.
3. **Python is the escape hatch.** Built-in evaluators cover common
   cases. When they don't fit, register a plugin (`evalguard.evaluators`
   entry-point) or drop a `custom_check: { module: my_pkg:my_func }` on
   any gate.
4. **Everything is versioned.** Prompts, datasets, schemas, rubrics,
   judges, heuristics each get a content-hashed `version_id` so a CI
   gate can compare apples to apples across PRs.
5. **Every action is audited.** Each run emits an append-only,
   hash-chained event log: who triggered it, which model judged what,
   what score it returned, which gate passed or failed. Tamper
   detection is one CLI call (`evalguard audit verify`); export is
   `jsonl`, W3C `prov-json`, or OpenTelemetry-flavoured `otel-json`.
6. **Local-first, server-optional.** The CLI runs entirely on your
   laptop with SQLite. The same config will run on a shared server
   later for org-wide visibility.

## Quickstart

```bash
uv pip install -e packages/cli -e packages/evaluators
cd examples/quickstart-summarizer
evalguard run                              # exits 0 — gate passes
```

Tighten a gate threshold or break a heuristic, run again, and the CLI
exits 2 with a coloured per-gate failure table. Drill into any row:

```bash
evalguard view                             # list recent runs
evalguard view <run_id>                    # rows table + per-layer rollup + gates
evalguard view <run_id> --row n1           # full per-row detail
evalguard view <run_id> --row n1 --layer 3 # just the L3 judge raw payload
```

## What's in `evalguard.yaml`

```yaml
version: 1
project: my-project

# Every value supports ${VAR} and ${VAR:-default}. Secrets stay out of repo.
providers:
  - id: openai:gpt-4o-mini
    config:
      api_key: ${OPENAI_API_KEY}
      temperature: 0.2                # any SDK kwarg passes through
      max_tokens: 512
      response_format: { type: json_object }
  - id: openai:llama3.2:3b            # any OpenAI-compatible local server
    config:
      base_url: ${OLLAMA_BASE_URL:-http://localhost:11434/v1}
      temperature: 0.0

prompts:
  - id: summarize_v1
    file: prompts/summarize.md        # content-hashed → version_id

datasets:
  - id: golden
    file: datasets/golden.jsonl       # rows: { id, input, expected, tags[] }
                                      # rag rows: { question, contexts[], expected_answer }
                                      # text_to_sql rows: { question, schema_ref, expected_sql, expected_result? }
                                      # any row may carry per-row overrides:
                                      #   provider: openai:gpt-4o
                                      #   params:   { temperature: 0.5 }     (SDK pass-through)
                                      #   retry:    { max_retries: 5 }       (operational)

# External systems an evaluator can reference via ``system: <name>``.
# Inlined into the evaluator's spec at YAML-load time so ``version_id``
# covers the binding. Used by the text_to_sql template's shadow-DB
# heuristics; ignored if no evaluator references it.
systems:
  shadow:
    kind:   sqlite
    url:    "${SHADOW_DB_URL:-sqlite:///./.evalguard/shadow.db}"
    schema: schemas/db.sql              # DDL bootstrapped before each row

# Run-wide retry policy for provider calls. Per-provider override:
# put ``retry:`` inside any provider's ``config:`` block. Per-row
# override: put ``retry:`` at the top level of a dataset row.
retry:
  max_retries: 3
  base_delay_ms: 1000
  max_delay_ms: 30000
  jitter: 0.25
  # retry_on: list of regex patterns matched (case-insensitively) against
  # f"{type(exc).__name__}: {exc}". Default covers 429 / 5xx / timeout /
  # connection-reset / temporarily-unavailable.

heuristics:                            # Layer 1 — cheap deterministic
  - { type: json_schema, schema_file: schemas/out.json }
  - { type: length, max: 600 }
  - { type: not_contains, value: "As an AI" }

metrics:                               # Layer 2 — RAG / semantic-similarity
  - { id: faithfulness,      type: faithfulness,      threshold: 0.5 }
  - { id: context_recall,    type: context_recall,    threshold: 0.5 }

judges:                                # Layer 3 — LLM-as-judge
  - id: helpfulness
    type: pointwise
    model: openai:gpt-4o
    rubric_file: rubrics/helpfulness.md
    threshold: 4.0
    params: { temperature: 0.0 }

run_mode: short_circuit_blocking_only    # save $ on rows that fail L1

layers:                                # one configurable gate per pyramid step
  heuristics:
    severity: block                    # block | warn | log
    aggregation: pass_rate
    threshold: { min: 1.0 }

  # Combine an absolute floor with a Δ-vs-baseline check on the same
  # gate. When ``evalguard run --baseline path.json`` is invoked,
  # ``min_delta_vs_baseline`` fires; without ``--baseline`` it's a no-op
  # so PR runs and local runs share the same YAML.
  judge_offline:
    severity: block
    aggregation: pass_rate_by_tag
    threshold:
      type: relative                   # absolute | relative | ttest
      min: 0.90                        # absolute floor (always enforced)
      min_delta_vs_baseline: -0.02     # fail if pass_rate dropped >2 pp vs baseline
      per_tag_overrides:
        safety:      1.00              # zero tolerance on safety
        helpfulness: 0.85              # adversarial cases get more slack

  # Statistical-threshold gate: Welch's two-sample t-test on per-evaluator
  # score samples. Requires ``--baseline`` (per-row scores live there) and
  # ``evaluator: <id>`` to scope the comparison.
  metrics:
    severity: warn
    evaluator: faithfulness
    threshold:
      type: ttest
      alpha: 0.05
      alternative: less                # fail if current is significantly LOWER than baseline
      min_n: 30                        # skip non-blockingly below this sample size

  human:                               # custom Python escape hatch
    severity: log
    custom_check:
      module: my_evals.gates:promote_disagreements
      config: { destination: ./goldens/candidates.jsonl }

cost_cap_usd: 2.00
```

## Providers shipped today

- **`mock`** — deterministic offline output (used by examples and CI)
- **`openai`** — OpenAI Chat Completions; `${OPENAI_API_KEY}` or `api_key`
  in YAML. Set `base_url` to point at **any OpenAI-compatible local server**
  (Ollama `/v1` shim, vLLM, LM Studio, llama.cpp `server`, LocalAI, Groq,
  Together, …). All SDK kwargs pass through.
- **`ollama`** — native Ollama API; `${OLLAMA_HOST}` or `base_url`.

Plugins register additional providers via the `evalguard.providers`
entry-point group.

## GitHub Action

Phase 1 ships a Docker action at `packages/action/` that wraps the CLI
and posts a sticky PR comment. Drop the example workflow at
`.github/workflows/evalguard-example.yml` into your own repo, point it
at your `evalguard.yaml`, and every PR gets a comment with per-trial
verdicts, gate results, and Δ-vs-baseline metrics.

```yaml
- uses: ./packages/action
  with:
    config: evalguard.yaml
    baseline:      ${{ github.event_name == 'pull_request' && './.baseline/baseline.json' || '' }}
    save_baseline: ${{ github.ref == 'refs/heads/main' && './baseline.json' || '' }}
```

See `packages/action/README.md` for the full input / output reference.

## Server (optional)

`apps/api/` ships a FastAPI server that accepts pushed runs and
serves them under the same JSON contract `view --json` produces.
Same config, two deployment modes — local-only (CLI + SQLite) or
shared (server + every CLI runner pushes to it for org-wide
visibility).

```bash
# Run the server
export EVALGUARD_API_KEY=$(openssl rand -hex 32)
uvicorn evalguard_api.main:app --host 0.0.0.0 --port 8787

# Point the CLI at it
export EVALGUARD_SERVER=https://evalguard.example.com
export EVALGUARD_API_TOKEN=$EVALGUARD_API_KEY
evalguard run                     # local
evalguard push --last             # → POST /v1/runs
```

`apps/api/README.md` has the full endpoint reference, configuration
matrix, and Docker quickstart. License: Apache-2.0 (server core),
separate from the MIT-licensed CLI.

## Status

- **Phase 0** — CLI, YAML loader with `${ENV}` substitution, local
  SQLite executor, content-addressable cache, pluggable evaluators
  (heuristics, metrics, judges) via Python entry points.
- **Phase 0.5** — Per-asset `version_id`, per-layer gates with
  severity / aggregation / threshold / `per_tag_overrides`, `run_mode`
  short-circuit, gate `custom_check` Python escape hatch, per-row
  drill-down view.
- **Phase 1** — Docker-based GitHub Action in `packages/action/`,
  sticky PR comments, relative thresholds, baseline save/load flow, and
  action-shape tests.
- **Phase 1c** — `evalguard push` no-op fallback for the future server,
  schema-drift canaries for `actor_type` / `severity` / `gate_status`.
- **Tier B (broaden eval surface)** — `rag` template + Layer-2 RAGAS-proxy
  metrics (`faithfulness`, `answer_relevancy`, `context_precision`,
  `context_recall`); `text_to_sql` template + three SQL heuristics
  (`sql_parses` via sqlglot, `dry_run_on_shadow_db` and
  `result_set_equivalence` via stdlib SQLite); top-level `systems:` block
  inlined into evaluator specs at YAML-load time so `version_id` covers
  the system binding; per-row `provider` / `params` overrides for
  stratified eval.
- **Tier C (production observability)** — bounded exponential-backoff
  provider retry with `provider.retry` / `provider.failed` audit events
  (rows mark failed without killing the trial); statistical-threshold
  gates via Welch's two-sample t-test (`threshold.type: ttest`) using
  per-evaluator score samples persisted in baseline files
  (`BASELINE_SCHEMA_VERSION = 1.1.0`).
- **Tier D (server skeleton)** — FastAPI app at `apps/api/` with
  `POST /v1/runs` (the real `evalguard push` target), `GET /v1/runs`
  (list), `GET /v1/runs/{id}` (full payload + server envelope). Single
  shared bearer auth or open mode for local dev; auto-generated OpenAPI
  + Swagger UI; SQLite default with `DATABASE_URL` seam ready for the
  Postgres port. Pydantic ingest model mirrors `evalguard.run.schema.json`
  with a drift canary roundtrip test.
- **Phase 2.5a (multi-tenancy made real)** — per-org API keys backed
  by the `api_keys` table (token = `evk_<32 hex>`, sha256-hashed at
  rest, plaintext returned exactly once on creation); `EVALGUARD_API_KEY`
  becomes an idempotent admin-key bootstrap. Org / Project / API-key
  CRUD endpoints. `/v1/runs` ingest scopes to caller's org; cross-org
  GET returns 404 (no enumeration leak); list endpoints silently scope
  to caller's org. Project ids are random opaque so `(org_id, slug)`
  is the uniqueness boundary — two orgs can both have a `demo` project.
- **Phase 2.5b (durability swap)** — query layer ported from
  hand-rolled `sqlite3` to SQLAlchemy 2.0 core (every helper / route
  uses `text()` with named binds; same code runs against SQLite or
  Postgres). Alembic ships migrations: `0001_initial` builds the
  schema from `MetaData`, `0002_rls_policies` is **Postgres-only** and
  enables Row-Level Security with `app.org_id` / `app.is_admin`
  session GUCs. Postgres support via the `[postgres]` install extra
  (`psycopg[binary]>=3.1`); the runtime auto-applies pending
  migrations on startup. The integration suite at
  `tests/api/test_postgres.py` is gated by `EVALGUARD_TEST_POSTGRES_URL`
  so SQLite-only contributors don't need a running pg.
- **Phase 2.6a (Next.js UI vertical slice)** — `apps/web/` Next.js 16
  + React 19 + Tailwind 4 SPA at the same JSON contract `view --json`
  produces. Static export (`output: 'export'`) — no Node runtime in
  prod; nginx Dockerfile included. Pages: **Settings** (server URL +
  bearer + `GET /v1/health` probe), **Runs** list (with project
  filter), **Run detail** (trials, gates, assets, per-trial gate
  table). `src/lib/api.ts` carries TypeScript mirrors of the
  `evalguard.run.schema.json` shapes so a contract drift surfaces as
  a type error. Bearer + server URL persist in `localStorage`; one
  static bundle deploys against staging or prod with no rebuild.
- **Phase 2.6b (UI tests + management surface)** — Vitest 3 + React
  Testing Library wired in (`happy-dom` runtime; 27 unit tests
  across `auth.ts`, the `api.ts` fetch wrapper, `Badge` /
  `statusTone`, and `ConnectionGate`). Three operator-facing
  pages: **Orgs** (list + admin-gated create), **Projects** (list +
  create in caller's org; admin can target other orgs via `?org_id=`),
  **Keys** (list + create with one-time token reveal + Copy button +
  two-click `ConfirmButton` revoke). Nav surfaces all five
  management routes. The TypeScript contract types extend with
  `Project`, `ApiKeySummary`, `ApiKeyCreated`.
- **Phase 2.6c (asset browse + polling refresh)** — new server
  endpoint `GET /v1/assets?kind=X&project=Y` aggregates per-run
  `assets[]` rows by `(project, kind, asset_id)` with
  `version_count` / `run_count` / `last_seen` / `last_run_id` /
  `last_version_id`. Scope respects org boundaries (member sees
  own; admin sees all). New `/assets/` web page with kind tabs
  (Datasets / Prompts / Judges / Heuristics / Metrics / Schemas
  / Rubrics) consuming it; rows link to the project's runs and
  to the most-recent run that produced each version. Runs list
  now polls every 10s via React Query's `refetchInterval` so
  freshly-pushed runs surface without a manual refresh.

## Coming next

| Phase | Deliverable |
|---|---|
| 1.5 | Published `evalguard/action@v1`; baseline registry polish |
| 2.6d | Asset detail page (versions over time + runs that used each); Playwright e2e |
| 3 | OTLP / `gen_ai.*` ingest; online sampler; drift detection |
| 4 | Argilla-style human review queue; κ tracking; promote-to-golden flow |
| 5 | Enterprise tier (SSO / SCIM / audit / dedicated) under ELv2 in `apps/api/ee/` |

The roadmap above summarizes the public milestones; implementation
details live alongside the shipped packages and tests in this repo.

## CLI

| Command | What it does |
|---|---|
| `evalguard init [-t text_gen\|rag\|text_to_sql]` | Scaffold a project |
| `evalguard validate [-c evalguard.yaml]` | Schema-validate + asset-resolve in <1s, no provider calls |
| `evalguard run [-c evalguard.yaml] [--baseline f.json] [--save-baseline f.json]` | Run pipeline; exit 0/2 by gate severity |
| `evalguard diff <run_a> <run_b>` | Side-by-side metric Δ between two local runs |
| `evalguard comment <run_id> [--baseline f.json] [--out file.md]` | Render a sticky PR-comment markdown body |
| `evalguard push <run_id\|--last> [--server URL] [--token TOK] [--dry-run]` | Upload a run to a remote EvalGuard server (no-op + hint when unconfigured) |
| `evalguard view` | List recent runs |
| `evalguard view <run_id>` | Rows table + per-layer rollup + gates |
| `evalguard view <run_id> --trial T` | Per-trial drill-down |
| `evalguard view <run_id> --row R [--layer N]` | Per-row drill-down |
| `evalguard view <run_id> --json [--scores] [--events]` | Stable JSON contract |
| `evalguard audit show <run_id> [--kind K]` | Render the audit timeline |
| `evalguard audit verify <run_id>` | Walk the per-run hash chain (exit 2 on tamper) |
| `evalguard audit export <run_id> -f jsonl\|prov-json\|otel-json` | Export for archival / OTel collector |

### Event vocabulary (W3C PROV "Activity")

| Kind | When |
|---|---|
| `run.started` | A new run begins |
| `asset.resolved` | A prompt / dataset / rubric / schema / judge / heuristic was loaded and content-hashed (one event per asset) |
| `run.cost_capped` | `cost_cap_usd` was hit; subsequent rows abort pre-flight |
| `run.finalized` | Final overall status is recorded |
| `trial.started` / `trial.finalized` | Per (provider × prompt) trial |
| `provider.called` | An LLM API call (the trial's main call **or** a judge's nested call — distinguishable via `payload.is_judge_call`) |
| `provider.retry` | A provider call failed with a retryable error; one event per retry attempt with `attempt`, `delay_ms`, `error_type`, `error` |
| `provider.failed` | A provider call exhausted its retry budget (or hit a non-retryable error). Carries the per-attempt summary; the row is recorded with empty output and the trial keeps running |
| `evaluator.heuristic.invoked` / `metric.invoked` / `judge.invoked` | An evaluator scored a row |
| `row.short_circuited` | A row failed an upstream block-severity layer; later layers were skipped |
| `gate.evaluated` | A gate was applied to a trial's metrics |
| `gate.custom_check.invoked` | A gate's Python `custom_check` ran (records module path, config, duration, pass/fail, exception if any) |

Exit codes: `0` pass · `2` blocking gate failed (or audit chain broken) · `1` infra/config error.

## Audit & governance

Every state change emits a typed, hash-chained event so corporate
deployments can answer *who did what, when, against which version,
with what inputs and outputs.* Every event also captures the **full
criteria the user set** — not just the resolved verdict — so an
auditor can answer:

- For an **LLM judge**: which model judged this row, what temperature /
  top_p / max_tokens, what threshold was the pass/fail criteria, which
  rubric (content-hashed `rubric_version_id`), what score did it parse,
  and what raw response did the model return.
- For a **gate**: what severity, what aggregation, what threshold,
  per-tag overrides, evaluator scope, custom Python check (if any).
- For a **heuristic / metric**: the full evaluator spec — e.g.
  `length: {max: 600, unit: chars}`, `not_contains: {value: "As an AI"}`.

API keys, tokens, passwords and similar secrets are stripped from
every event payload **before** the hash is computed (key match is
case-insensitive: `api_key`, `Authorization`, `bearer_token`, …),
so the chain still verifies and a leaked YAML config doesn't leak
into the audit log.

Vocabulary maps to W3C PROV (Activity / Entity / Agent). Field
naming is OpenTelemetry-GenAI compatible so the same data ports
cleanly to OTLP later. Hash chain is per-`run_id` (single-writer
hash chain — sufficient for self-host; Sigstore-Rekor / Merkle
upgrade lives on the enterprise tier).

Privacy: `audit.redact_payload: true` strips rendered prompts,
raw responses, and judge reasons from the payload but **keeps their
content hashes** so the chain still verifies after erasure — the
pattern Sentry / Langfuse use to reconcile append-only logs with
GDPR right-to-erasure.

```yaml
audit:
  redact_payload: false     # set true to comply with PII / PHI policies
```

```text
$ evalguard audit show run_abc --kind evaluator.judge.invoked
Audit timeline · run_abc (10 events)
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━┳━━━━━━━━━━┳━━━━━┳━━━━━┳━━━━━━┳━━━━━━┓
┃ at           ┃ kind                    ┃ s… ┃ trial    ┃ row ┃ d_ms┃ cost ┃ hash ┃
┡──────────────╇─────────────────────────╇────╇──────────╇─────╇─────╇──────╇──────┩
│ 12:00:00.583 │ evaluator.judge.invoked │ q  │ trial_a… │ n1  │  17 │      │ fdd5… │
│ ...                                                                              │
actor: ci:gh:acme/widgets#run/12345  (ci)

$ evalguard audit verify run_abc
✓ chain intact · 60 events · run run_abc

$ evalguard audit export run_abc -f otel-json | otel-collector ingest
```

## Repo layout

```
apps/
  api/                FastAPI server (Apache-2.0): /v1/runs ingest + read
  web/                Next.js 16 SPA (Apache-2.0): Runs + Settings pages,
                      static export, deploys behind nginx or any static host
                      (planned: worker)
packages/
  cli/                evalguard CLI + local executor
  evaluators/         heuristics, metrics, judges, providers
  schemas/            evalguard.yaml + run + baseline JSON schemas
  templates/          starter scaffolds (text_gen, rag, text_to_sql)
  action/             Phase-1 Docker GitHub Action
examples/
  quickstart-summarizer/
tests/                pytest — loader, cache, gate, judges, executor,
                      env subst, custom_check, layered gates, retry,
                      stats / ttest, shadow-DB, schema drift, push, …
  api/                pytest — FastAPI: health, auth, runs roundtrip,
                      conflict, list, pydantic drift canary
```

## License

- **MIT** — `packages/cli`, `packages/evaluators`, SDKs
- **Apache-2.0** — server core (when added)
- **Elastic License v2** — enterprise modules under `apps/api/ee/` (when added)

The same playbook as Sentry / PostHog / Langfuse.
