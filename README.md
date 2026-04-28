# EvalGuard

> Open-source, modular evaluation platform for LLM systems. Local-first CLI today; multi-tenant server, CI/CD gates, online tracing, and human review on the same data model.

EvalGuard implements the 5-layer evaluation framework — heuristics, semantic
metrics, offline LLM-as-judge, online LLM-as-judge, human spot checks — as
declarative YAML you control without touching engine code.

## Status

**Phase 0 — local CLI MVP.** `evalguard run` executes a YAML-defined eval
against an in-memory pipeline with content-addressable caching, persists runs
to SQLite, evaluates gates, and exits with CI-friendly codes.

Roadmap (see `/root/.claude/plans/immutable-launching-barto.md`):
- Phase 1 — GitHub Action with sticky PR comments and score deltas
- Phase 2 — FastAPI server + Next.js UI
- Phase 3 — OTLP ingest, online judges, drift detection
- Phase 4 — Human review queue, κ tracking, promote-to-golden
- Phase 5 — Enterprise SSO/SCIM/audit
- Phase 6 — Multimodal (vision, agent trajectory)

## Quickstart

```bash
# install in dev mode
uv pip install -e packages/cli -e packages/evaluators

# run the example (uses mock provider + mock judge — no API key required)
cd examples/quickstart-summarizer
evalguard run

# tighten the gate to force a regression
sed -i 's/value: 4.0/value: 4.9/' evalguard.yaml
evalguard run    # exit 2 = gate fail
```

## Providers

EvalGuard ships with three providers and any plugin can register more:

- `mock` — deterministic offline output. Default for examples and CI.
- `openai` — OpenAI Chat Completions. Set `OPENAI_API_KEY` (or `api_key` in
  YAML). Pass a custom `base_url` to point at any **OpenAI-compatible local
  server** (Ollama's `/v1` shim, vLLM, LM Studio, llama.cpp's `server`,
  LocalAI, Together AI, Groq, …).
- `ollama` — native Ollama API at `http://localhost:11434` by default
  (override via `OLLAMA_HOST` or `base_url`).

Local-model example:

```yaml
providers:
  - id: ollama:llama3.2:3b
    config: { base_url: http://localhost:11434 }
  # or via OpenAI-compatible shim:
  - id: openai:llama3.2:3b
    config: { base_url: http://localhost:11434/v1 }
```

## Layout

```
packages/
  cli/         evalguard CLI + local SQLite executor
  evaluators/  built-in heuristics, judges, providers
  schemas/     evalguard.yaml JSON schema
  templates/   text_gen, rag, text_to_sql scaffolds
examples/
  quickstart-summarizer/
```

## License

- Apache-2.0 — server core (when added)
- MIT — `packages/cli`, `packages/evaluators`, SDKs
- Elastic License v2 — enterprise modules under `apps/api/ee/` (when added)
