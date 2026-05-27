# Golden dataset workflow

A *golden dataset* is your curated set of ground-truth `(input,
expected)` rows. EvalGuard lets you build it by promoting real calls
you've inspected — bad ones to add as regression cases, good ones to
lock in as known-correct behaviour — instead of authoring rows by
hand.

The loop, end to end:

```
   /calls/  (a run's rows)            promote                /golden/  (DB view)
   ─────────────────────── click ───────────────► ───────────────────────────────
   see input / output / scores                    inline preview · search · sort
   spot a row worth curating                       bulk select · Download JSONL
                                                            │
                          evalguard golden export ──────────┤  (CLI, for CI)
                                                            ▼
                                          datasets/golden.jsonl on disk
                                                            │
                                            evalguard.yaml `datasets:` ⇒ next run
```

## 1. Find a call worth curating

Open `/calls/?project=<slug>` in the UI. The stream is cursor-
paginated and has two tabs:

- **Recent** — newest calls first (wall-clock `ingested_at`).
- **Failures** — only rows the automated gates marked failed.

Click any row to open the detail panel: input, expected, output,
per-evaluator scores, and the gate verdicts that applied.

## 2. Promote it

The detail panel's **Promote to golden** button stages the row in a
server-side `golden_candidates` table:

```
POST /v1/golden/candidates   { run_id, row_id, note? }
```

Promotion is idempotent per reviewer — re-clicking updates your note
rather than creating a duplicate, and two reviewers promoting the
same row produce two independent records (so cross-annotator
agreement can be computed later). The row's `(input, expected,
output)` are NOT copied at promote time — they're resolved live from
the run when you view or export, so a re-ingested run's corrections
flow through.

## 3. Curate in the golden database view

`/golden/?project=<slug>` is the curation surface:

| Feature | What it does |
|---|---|
| Project picker | Switch projects without editing the URL |
| Inline preview | Expand any row (▸) to see input / expected / output without leaving the page |
| Search | Free-text across row id / run id / reviewer / note **and** row content (incl. structured/object inputs) |
| Sort | By When (default, newest-first) / Reviewer / Row |
| Bulk select | Select-all + per-row checkboxes |
| Download JSONL | Compose the dataset file in-browser (no terminal needed) |
| Download selected | Same, for just the checked rows |
| Remove / Remove selected | Un-promote (only your own promotions, or admin) |
| Copy | One row's JSONL line to the clipboard |

The view fetches with `?expand=row` so the content is available
client-side for preview + download in a single request.

## 4. Export to a dataset file

Two equivalent paths produce **byte-identical** JSONL:

**In-browser** — the *Download JSONL* button.

**CLI** (for CI / scripted exports):

```bash
export EVALGUARD_SERVER=https://evalguard.example.com
export EVALGUARD_API_TOKEN=evk_...

evalguard golden list   --project customer-service
evalguard golden export --project customer-service --to datasets/golden.jsonl
evalguard golden export --project customer-service --to datasets/golden.jsonl --mode merge
```

`--mode overwrite` (default) truncates the target; `--mode merge`
appends, skipping rows whose `id` already exists in the file. An
`overwrite` with zero candidates refuses to truncate an existing
file (foot-gun guard).

### JSONL row shape

One row per line, keys sorted (diffable across re-exports):

```json
{
  "_provenance": {
    "created_at": "2026-05-21T07:30:00",
    "note": "model apologised but didn't offer a refund",
    "promoted_by": "key_abc123",
    "run_id": "run_xxxxxxxx"
  },
  "expected": "apologise and offer a refund",
  "id": "r-fail-42",
  "input": "Why was my order late?"
}
```

**`output` is deliberately excluded.** A golden row is the
`(input, expected)` ground-truth pair — `expected` is the right
answer, not what the model produced. The model's `output` is the
thing *under test*; including it in ground truth would be
circular. The UI preview shows `output` so a reviewer can judge it;
the export does not.

Rows whose `input` is `null` (some OTLP-derived rows lack one) are
skipped from the export with a reported count — a JSONL line with
`"input": null` would crash a downstream evaluator.

## 5. Feed it back

Reference the file in `evalguard.yaml`:

```yaml
datasets:
  - id: golden
    file: datasets/golden.jsonl
```

The next `evalguard run` evaluates against it. Because each dataset
gets a content-hashed `version_id`, a CI gate can compare apples to
apples as the golden set grows.

## Tenancy + audit

- All golden endpoints are tenant-scoped: a member only sees / writes
  candidates for their own org's projects; a cross-org request 404s
  (same anti-enumeration shape as `GET /v1/runs/{id}`).
- Un-promote (`DELETE /v1/golden/candidates/{id}`) is restricted to
  the original promoter or an admin.
- Every promote / un-promote is a bearer-authed request and lands in
  the structured access log (`{evt: http.request, key_id, org_id,
  path, status}`), so "who curated what when" is reconstructable.

## Related

- [`docs/observability.md`](observability.md) — the `/calls/` stream the promotions come from.
- `apps/api/README.md` — full endpoint reference.
- `apps/web/README.md` — UI architecture.
