# EvalGuard docs

Feature-level documentation. The top-level [`README.md`](../README.md)
is the project overview + roadmap; these pages go deep on individual
surfaces.

| Doc | Covers |
|---|---|
| [observability.md](observability.md) | Per-call stream (`/calls/`), live timeline + aggregate, OTLP ingest, drift, audit chain (`/audit/events`, `/audit/verify`), sampler |
| [golden-dataset.md](golden-dataset.md) | Promote-to-golden loop, `/golden/` DB view, `evalguard golden` CLI export |

The proxy gateway (`POST /v1/projects/{slug}/invoke`) and the
per-project YAML config workflow (`/v1/projects/{slug}/config*` +
the `/config` web page) are documented inside the app READMEs:

- [`apps/api/README.md` — Gateway use (Phase PROXY)](../apps/api/README.md#gateway-use-phase-proxy)
  — endpoint contract, audit-chain shape, rate-limit / cost-cap
  semantics.
- [`apps/web/README.md` — Page tour](../apps/web/README.md#page-tour)
  — the `/config` editor + `/calls` live stream + LiveTimeline.

Component-level docs live next to their code:

- [`apps/api/README.md`](../apps/api/README.md) — server endpoints, auth, config, deploy.
- [`apps/web/README.md`](../apps/web/README.md) — UI architecture, routing, test pyramid.
- [`packages/action/README.md`](../packages/action/README.md) — the `evalguard/action@v1` GitHub Action.
