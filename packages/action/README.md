# `evalguard/action@v1`

A GitHub Action that runs EvalGuard in CI, pushes the run to a
hosted server, and gates the workflow on the verdict. Optionally
posts a sticky PR comment with per-trial verdicts, gate results, and
Δ-vs-baseline metrics.

## TL;DR — three flows

| Flow | Use when | Inputs |
|---|---|---|
| **Run + gate, no server** | Local CI, no hosted backend yet | `config` only |
| **Run + push + gate** | You have a Phase-2 server | + `server`, `token` |
| **Run + PR comment + baseline diff** | You want PR-level UX | + `baseline`, `save_baseline` |

All three combine — push to a server *and* comment on the PR is fine.

The minimal "run + push + gate" example lives at
[`examples/server-push.yml`](examples/server-push.yml).  The richer
baseline-diff flow is below.

## Quickstart

```yaml
# .github/workflows/evalguard.yml
name: EvalGuard
on:
  push:
    branches: [main]
  pull_request:

jobs:
  eval:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write   # for the sticky PR comment
      contents: read
    steps:
      - uses: actions/checkout@v4

      # ── PR runs: pull main's baseline if one exists ───────────────────
      - name: Download baseline (PR runs)
        if: github.event_name == 'pull_request'
        uses: dawidd6/action-download-artifact@v6
        with:
          workflow: evalguard.yml
          branch: main
          name: evalguard-baseline
          path: ./.baseline
        continue-on-error: true

      - name: Run EvalGuard
        id: evalguard
        uses: ./packages/action          # or: evalguard/action@v1
        with:
          config: evalguard.yaml
          # On PR: compare against the downloaded main baseline.
          baseline: ${{ github.event_name == 'pull_request' && './.baseline/baseline.json' || '' }}
          # On main: write a fresh baseline.
          save_baseline: ${{ github.ref == 'refs/heads/main' && './baseline.json' || '' }}

      # ── Main runs: upload the freshly-saved baseline ──────────────────
      - name: Upload baseline (main only)
        if: github.ref == 'refs/heads/main'
        uses: actions/upload-artifact@v4
        with:
          name: evalguard-baseline
          path: ./baseline.json
          retention-days: 30
```

## What the Action does

1. **`evalguard validate`** — schema-validates the config and resolves
   every asset (prompts, datasets, rubrics, schemas, judges,
   heuristics) in <1 s. Bails with exit 1 before spending money on
   provider calls.
2. **`evalguard run`** — executes the eval pipeline. Passes
   `--baseline` if the input is set; passes `--save-baseline` to
   snapshot the run on main.
3. **`evalguard comment`** — renders a sticky markdown body from the
   stable JSON contract the CLI exposes. Sections include trials,
   gates, and (when a baseline is provided) Δ-vs-baseline.
4. **GitHub REST API** — finds an existing comment carrying the marker
   (default `<!-- evalguard:pr-comment -->`) and PATCHes it; POSTs a
   new comment if none exists. So PRs get one ever-updating comment,
   not a flood.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `config` | `evalguard.yaml` | Path to your config |
| `baseline` | _empty_ | Baseline JSON for `relative` gates + Δ table |
| `save_baseline` | _empty_ | When set, snapshot at end of run |
| `comment` | `true` | Set `false` to skip the PR comment |
| `marker` | `<!-- evalguard:pr-comment -->` | Override to host multiple stickies on one PR |
| `pr_number` | auto | Auto-detected on `pull_request` events |
| `github_token` | `${{ github.token }}` | For posting comments |
| `fail_fast` | `false` | Pass `--fail-fast` to `evalguard run` |
| `server` | _empty_ | EvalGuard server URL. Push happens when this + `token` are both set. |
| `token`  | _empty_ | Bearer token. Use `${{ secrets.EVALGUARD_TOKEN }}` — never inline. |
| `push`   | `true` | Set `false` to run locally even when secrets are present. |
| `fail_on` | `gate_failed` | Comma-separated `gate_status` values that fail the workflow. `never` to disable. |

## Outputs

| Output | Notes |
|---|---|
| `exit_code` | `0` pass · `2` blocking gate failed · `1` infra error (raw exit of `evalguard run`) |
| `run_id` | The new run's id (e.g. `run_abc123…`) |
| `comment_url` | URL of the posted PR comment, if any |
| `gate_status` | `passed` / `warned` / `gate_failed` / `row_failed` / `cost_capped` (or empty if the run never produced JSON) |
| `cost_usd` | Total run cost (string, four decimals) |
| `url` | Server-side URL for the run when `push` succeeded |

## Exit codes

The Action's exit code follows `fail_on`, not `evalguard run` directly:

- **Infra errors always fail.**  If `evalguard run` exited non-zero
  AND there's no parseable `gate_status` to inspect (e.g., crashed
  during model load, malformed config), the action exits with the
  same code.  `fail_on` doesn't gate infra errors.
- **Gate results filter through `fail_on`.**  The parsed
  `gate_status` is compared against the comma-separated `fail_on`
  list.  Default `gate_failed` keeps the v0 behaviour where only
  blocking failures fail CI; widen to `gate_failed,warned` to fail
  on warnings, or set `fail_on: never` for advisory-only runs.

## Self-host vs published action

When pinned at `evalguard/action@v1` the Docker image is pulled from
GHCR.  Self-hosting (private fork / air-gapped) is supported via
the `./packages/action` path in all examples — the Dockerfile builds
from the source tree and never reaches out.
