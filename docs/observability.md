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
| **Proxy gateway** | `POST /v1/projects/{slug}/invoke` records each call as it serves; one row of a lazy-created daily live run (`run_live_<sha>`) | `live` |

The OTLP path can be load-shed at the edge with
`EVALGUARD_OTLP_SAMPLE_RATE` (deterministic head-based sampling on
`traceId` — all spans of a trace share the verdict).

## The stream — `GET /v1/projects/{slug}/calls`

```
GET /v1/projects/{slug}/calls?tab=recent|failures|passed&cursor=&limit=&source=cli|otlp|live&from=&to=
```

- **Cursor pagination** — opaque base64 cursor; seek-after on
  `(ingested_at DESC, id DESC)` so a batch ingested in the same
  millisecond paginates without skips or duplicates. A legacy row
  with `NULL ingested_at` paginates by `id` alone (id-only cursor)
  rather than terminating the stream.
- **Tabs** — `recent` (newest first), `failures` (`passed = 0`
  only), or `passed` (`passed = 1` only); the filter holds on every
  page, not just page 1.
- **`?source=cli|otlp|live`** — case-insensitive; composes with the
  tab.
- **`?from=&to=`** — half-open window `[from, to)` on `ingested_at`,
  used by the LiveTimeline strip to scope the stream to a specific
  day.  Both are ISO-8601.
- Each item carries `passed`, `cost_usd`, `latency_ms`, `tags`,
  `ingested_at`, and a 240-char `output_preview`.

The index `idx_run_rows_calls (project_id, ingested_at DESC, id DESC)`
backs the query so the planner does a forward seek (not a backward
scan + sort) on Postgres.

## Live timeline — daily rollups

Two endpoints power the `LiveTimeline` strip the `/calls/` page
renders above the stream:

```
GET /v1/projects/{slug}/live/timeline?days=30    # max 90
GET /v1/projects/{slug}/live/aggregate[?from=&to=]
```

- **`/live/timeline`** returns one entry per live `run_live_*`
  newest-first, carrying `(run_id, started_at, row_count,
  row_pass_count, row_fail_count, cost_usd, finished_at)`.  The
  UI pads to a contiguous 30-day window client-side so silent
  days render as hairlines rather than misleading "yesterday is
  next to a week ago" gaps.
- **`/live/aggregate`** sums pass / fail / cost / run count across
  the optional `[from, to)` window (omit the bounds for all-time).
  Used to drive the "1,243 calls · 99.2% pass · $4.10" banner above
  the stream.

## Audit chain — `GET /v1/projects/{slug}/audit/events` + `/audit/verify`

Every `/invoke` call appends one row to the `event_rows` table.
The chain invariant — `prev_event_hash` of event N equals
`event_hash` of event N-1 — is enforced at the DB layer via
`UNIQUE (run_id, prev_event_hash)` + a partial unique index
`(run_id) WHERE prev_event_hash IS NULL` so a concurrent
first-of-day insert can't fork the chain root.

```
GET /v1/projects/{slug}/audit/events?run_id=run_live_…&limit=500
GET /v1/projects/{slug}/audit/verify?run_id=run_live_…
```

- **`/audit/events`** lists events in chain order; response carries
  `corrupt_rows` + `truncated` so a partial chain surfaces clearly.
- **`/audit/verify`** walks the chain and verifies every link.
  Returns the same `{ok, events, broken_at, reason}` shape the CLI's
  `verify_chain` returns — operators reading one log can parse the
  other.  Refuses (`413`) when the chain exceeds the verify page
  cap rather than reporting `ok=true` for a visible prefix; cursor
  pagination for long-chain verify is on the roadmap.

The same `build_event` / `verify_chain_events` helpers from
`evalguard_evaluators.audit` that back the CLI's append-only log
back this surface, so a tampered byte in either place fails
verification the same way.

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
