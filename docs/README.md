# EvalGuard docs

Feature-level documentation. The top-level [`README.md`](../README.md)
is the project overview + roadmap; these pages go deep on individual
surfaces.

| Doc | Covers |
|---|---|
| [observability.md](observability.md) | Per-call stream (`/calls/`), OTLP ingest, drift, sampler |
| [golden-dataset.md](golden-dataset.md) | Promote-to-golden loop, `/golden/` DB view, `evalguard golden` CLI export |

Component-level docs live next to their code:

- [`apps/api/README.md`](../apps/api/README.md) — server endpoints, auth, config, deploy.
- [`apps/web/README.md`](../apps/web/README.md) — UI architecture, routing, test pyramid.
- [`packages/action/README.md`](../packages/action/README.md) — the `evalguard/action@v1` GitHub Action.
