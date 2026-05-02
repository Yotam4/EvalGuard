# `evalguard/action@v1`

A GitHub Action that runs EvalGuard on every PR and posts a sticky
markdown comment with per-trial verdicts, gate results, and
Δ-vs-baseline metrics.

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

## Outputs

| Output | Notes |
|---|---|
| `exit_code` | `0` pass · `2` blocking gate failed · `1` infra error |
| `run_id` | The new run's id (e.g. `run_abc123…`) |
| `comment_url` | URL of the posted PR comment, if any |

## Exit codes

The Action exits with the same code as `evalguard run`. A blocking
gate failure (exit 2) fails the workflow step — making the check
red on the PR — even if the comment posted successfully.
