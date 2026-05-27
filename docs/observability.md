# Per-call observability

EvalGuard's batch model is a *run* = a CI invocation with trials and
rows. The observability surface flips that: a *call* is one LLM
request→response, and the `/calls/` stream lets you scan thousands of
them across a project and drill into any single one.

This is the surface a customer-service team uses to answer "did the
500 calls our config handled today behave?" without opening 500
individual runs.

## Getting calls in

Three ingest paths, all land as rows the stream reads:

| Path | How | `source` |
|---|---|---|
| `evalguard push` | Run locally / in CI, push the batch | `cli` |
| OTLP traces | Your app emits OpenTelemetry GenAI spans → `POST /v1/otlp/v1/traces` | `otlp` |
| (future) proxy | `POST /v1/projects/{slug}/invoke` records as it serves | — |

The OTLP path can be load-shed at the edge with
`EVALGUARD_OTLP_SAMPLE_RATE` (deterministic head-based sampling on
`traceId` — all spans of a trace share the verdict).

## The stream — `GET /v1/projects/{slug}/calls`

```
GET /v1/projects/{slug}/calls?tab=recent|failures&cursor=&limit=&source=
```

- **Cursor pagination** — opaque base64 cursor; seek-after on
  `(ingested_at DESC, id DESC)` so a batch ingested in the same
  millisecond paginates without skips or duplicates. A legacy row
  with `NULL ingested_at` paginates by `id` alone (id-only cursor)
  rather than terminating the stream.
- **Tabs** — `recent` (newest first) or `failures` (`passed = 0`
  only; the filter holds on every page, not just page 1).
- **`?source=cli|otlp`** — case-insensitive; composes with the tab.
- Each item carries `passed`, `cost_usd`, `latency_ms`, `tags`,
  `ingested_at`, and a 240-char `output_preview`.

The index `idx_run_rows_calls (project_id, ingested_at DESC, id DESC)`
backs the query so the planner does a forward seek (not a backward
scan + sort) on Postgres.

## Per-call detail — `GET /v1/projects/{slug}/calls/{run_id}/{row_id}`

Returns the full row: input, expected, output, per-evaluator scores,
and the trial's gate verdicts. The content is parsed live from the
run's `payload_json` (kept out of the stream paginator's hot path).

## The UI

`/calls/?project=<slug>&tab=recent` — a virtualized list (handles
10k+ rows) of pass/fail-badged cards. Click a card → side panel with
the full detail + a **Promote to golden** button (see
[`docs/golden-dataset.md`](golden-dataset.md)).

Rows carry `data-testid="call-card"` + `data-run-id` + `data-row-id`
for stable e2e addressing.

## Tenancy

Every endpoint is tenant-scoped via a project-visibility check:
a member only reaches their own org's projects; admin reaches any.
Cross-org / missing → 404 (never 403 — no existence leak).

## Related

- [`docs/golden-dataset.md`](golden-dataset.md) — promote calls into ground truth.
- `apps/api/README.md` — full endpoint reference.
