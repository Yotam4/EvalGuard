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

heuristics:                            # Layer 1 — cheap deterministic
  - { type: json_schema, schema_file: schemas/out.json }
  - { type: length, max: 600 }
  - { type: not_contains, value: "As an AI" }

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

  judge_offline:
    severity: block
    aggregation: pass_rate_by_tag
    threshold:
      min: 0.90
      per_tag_overrides:
        safety:      1.00              # zero tolerance on safety
        helpfulness: 0.85              # adversarial cases get more slack

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

## Status

- **Phase 0** — CLI, YAML loader with `${ENV}` substitution, local
  SQLite executor, content-addressable cache, pluggable evaluators
  (heuristics, metrics, judges) via Python entry points.
- **Phase 0.5** — Per-asset `version_id`, per-layer gates with
  severity / aggregation / threshold / `per_tag_overrides`, `run_mode`
  short-circuit, gate `custom_check` Python escape hatch, per-row
  drill-down view.

## Coming next

| Phase | Deliverable |
|---|---|
| 1 | `evalguard/action@v1` GitHub Action; sticky PR comments; `relative` & `statistical` thresholds; baseline registry |
| 2 | Optional FastAPI server (multi-project, RBAC, Next.js UI mirroring YAML control panels) |
| 3 | OTLP / `gen_ai.*` ingest; online sampler; drift detection |
| 4 | Argilla-style human review queue; κ tracking; promote-to-golden flow |
| 5 | Enterprise tier (SSO / SCIM / audit / dedicated) under ELv2 in `apps/api/ee/` |

See `/root/.claude/plans/immutable-launching-barto.md` for the full
design doc and roadmap.

## CLI

| Command | What it does |
|---|---|
| `evalguard init [-t text_gen\|rag\|text_to_sql]` | Scaffold a project |
| `evalguard run [-c evalguard.yaml]` | Run pipeline; exit 0/2 by gate severity |
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
| `run.cost_capped` | `cost_cap_usd` was hit; subsequent rows abort pre-flight |
| `run.finalized` | Final overall status is recorded |
| `trial.started` / `trial.finalized` | Per (provider × prompt) trial |
| `provider.called` | An LLM API call (the trial's main call **or** a judge's nested call — distinguishable via `payload.is_judge_call`) |
| `evaluator.heuristic.invoked` / `metric.invoked` / `judge.invoked` | An evaluator scored a row |
| `row.short_circuited` | A row failed an upstream block-severity layer; later layers were skipped |
| `gate.evaluated` | A gate was applied to a trial's metrics |

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
apps/                 (planned: api, web, worker)
packages/
  cli/                evalguard CLI + local executor
  evaluators/         heuristics, metrics, judges, providers
  schemas/            evalguard.yaml JSON schema
  templates/          starter scaffolds (text_gen; rag/text_to_sql planned)
  action/             (planned) GitHub Action
examples/
  quickstart-summarizer/
tests/                pytest — yaml loader, cache, gate, judges, executor,
                      env subst, custom_check, layered gates
```

## License

- **MIT** — `packages/cli`, `packages/evaluators`, SDKs
- **Apache-2.0** — server core (when added)
- **Elastic License v2** — enterprise modules under `apps/api/ee/` (when added)

The same playbook as Sentry / PostHog / Langfuse.
