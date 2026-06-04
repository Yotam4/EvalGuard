# Gates

A **gate** is a policy assertion EvalGuard applies to a run.  It takes
the run's measured metrics, compares them to a threshold, and emits a
verdict the rest of the system reads — the CLI's exit code, the
sticky PR comment, the `gate_status` column on `/v1/runs`, the
"Review N failures →" link on `/runs/detail`, and the audit
`gate.evaluated` event.

Gates are how "the eval ran" becomes "the eval *passed*".

```yaml
layers:
  heuristics:
    severity: block             # how the verdict surfaces
    aggregation: pass_rate      # what number to look at
    threshold:
      min: 1.0                  # what the number must satisfy
```

That's the entire mental model: **severity + aggregation + threshold**.
Everything else (`per_tag_overrides`, `custom_check`, `rules`,
relative / t-test threshold types) is an extension of one of those
three knobs.

---

## The two YAML shapes

Two shapes are accepted simultaneously by `evaluate_gates` in
`packages/cli/evalguard_cli/local/gate.py`.  Both produce the same
`GateResult` list; mix them freely.

### Per-layer (canonical, Phase 0.5+)

```yaml
layers:
  heuristics: { severity: block, threshold: { min: 1.0 } }
  judge_offline:
    severity: block
    aggregation: pass_rate_by_tag
    threshold:
      type: relative
      min: 0.90
      min_delta_vs_baseline: -0.02
      per_tag_overrides: { safety: 1.00 }
```

One gate per pyramid step.  Each step is independently controllable.
This is the shape every new doc / example uses.

### Legacy global (Phase 0 back-compat)

```yaml
gates:
  - name: minimum_quality
    severity: block             # or "blocking: true" — both accepted
    rules:
      - { metric: pass_rate,            op: ">=", value: 0.95 }
      - { metric: judge_helpfulness_mean, op: ">=", value: 4.0 }
```

Rules use the six comparison ops `>=`, `<=`, `>`, `<`, `==`, `!=`
against any top-level metric the run emitted.  Kept for back-compat;
prefer per-layer for new configs.

---

## The five layers

The pyramid runs cheap → expensive.  Each layer name is a stable
key under `layers:` (drift-tested against the JSON schema).

| Layer | Index | Evaluator examples | Typical cost |
|---|---|---|---|
| `heuristics`    | 1 | `json_schema`, `length`, `not_contains`, `sql_parses`, `dry_run_on_shadow_db`, `result_set_equivalence` | µs–ms, no LLM |
| `metrics`       | 2 | `lex.faithfulness`, `lex.answer_relevancy`, `lex.context_precision_unranked`, `lex.context_recall` | ms, lexical only (no LLM) |
| `judge_offline` | 3 | `judge.pointwise`, `judge.mock_pointwise` (LLM-as-judge, blocking layer) | seconds, billed LLM call |
| `judge_online`  | 4 | Same evaluators as L3, treated as warn-only by convention | seconds, billed LLM call |
| `human`         | 5 | Custom Python check + the `/reviews` queue feeding back | minutes–hours |

Layer numbers are the `layer` int on a `GateResult` and the `layer`
column on `gate.evaluated` audit events.  Evaluator types come from
`evalguard.evaluators` entry points (heuristics / metrics / judges
registered in `packages/evaluators/pyproject.toml`).

---

## Severity → CLI behaviour

```text
block — gate failure exits non-zero (CLI: 2; PR comment: "FAIL")
warn  — gate failure exits 0 + emits a warning row
log   — gate failure is recorded only; never surfaces in PR/CI status
```

Severity is also the lens through which the run-level `gate_status`
is computed: a single `block` failure on any trial flips the run to
`failed`; any `warn` failure (no `block` failures) flips to `warned`;
otherwise `passed`; with no gates configured at all, `none`.

A `log`-severity failure never changes `gate_status` — it's the
"telemetry only" knob.

Exit-code mapping (CLI):

```
0  — pass (no block failure; warn failures allowed)
2  — block failure OR audit chain broken
1  — infra / config error (YAML invalid, missing file, etc.)
```

---

## Aggregations

The aggregation is **what number the gate looks at**.  Five are
implemented; the JSON schema additionally advertises `p50` / `p95`
for forward-compatibility (a drift test pins the gap so users get
a clear "not implemented" error rather than silent fail-closed).

| `aggregation` | Reads from | What it is |
|---|---|---|
| `pass_rate` (default)  | layer rollup or per-evaluator | `passed_score_count / total_score_count` |
| `row_pass_rate`        | layer rollup                  | `passed_row_count / total_row_count` (counts a row passed if all its scores on the layer pass) |
| `mean`                 | layer rollup or per-evaluator | arithmetic mean of the numeric score |
| `count_failures`       | layer rollup or per-evaluator | absolute count of failed scores |
| `pass_rate_by_tag`     | `by_tag` rollup               | mean of per-tag `pass_rate` (combine with `per_tag_overrides` to enforce different floors per tag) |
| `p50`, `p95`           | *(reserved)*                  | not implemented — unknown aggregation → 422-like gate failure with a clear message |

Scoping to one evaluator (instead of the whole layer):

```yaml
metrics:
  severity: warn
  evaluator: lex.faithfulness          # scope to this specific evaluator
  aggregation: mean
  threshold: { min: 0.6 }
```

When `evaluator:` is set the gate reads `by_evaluator[<id>][<agg>]`
instead of `by_layer[<idx>][<agg>]`.  Useful when one layer has
several evaluators with different sensitivities.

---

## Thresholds

The threshold block is the **rule** the aggregation's value has to
satisfy.  Three types; `absolute` is the default.

### `type: absolute` (default)

```yaml
threshold: { min: 0.95, max: 0.05 }
```

`min` is `actual >= min`, `max` is `actual <= max`.  Both can appear
together (e.g. "pass rate between 0.95 and 0.99").

### `type: relative` (needs a baseline)

```yaml
threshold:
  type: relative
  min: 0.95                              # absolute floor (still enforced)
  min_delta_vs_baseline: -0.02           # fail if pass-rate dropped > 2pp vs baseline
  max_delta_vs_baseline:  0.10           # fail if pass-rate jumped  >10pp vs baseline
```

`min_delta_vs_baseline` and `max_delta_vs_baseline` compare
`actual − baseline_actual` to the configured number.  The baseline
comes from `evalguard run --baseline path/to/baseline.json`; without
`--baseline` the relative checks are skipped non-blockingly so PR
runs and local runs share the same YAML.  Combine with `min:` /
`max:` to enforce an absolute floor as a backstop in case the
baseline itself was a regression.

### `type: ttest` (statistical, also needs a baseline)

```yaml
metrics:
  severity: warn
  evaluator: lex.faithfulness            # ttest REQUIRES evaluator: <id>
  threshold:
    type: ttest
    alpha: 0.05                          # significance level
    alternative: less                    # "less" → fail when current is significantly lower than baseline
    min_n: 30                            # below this on either side, skip non-blockingly
```

Welch's two-sample t-test on the per-evaluator score samples, current
run vs baseline run.  Samples live under `metrics.samples[<evaluator_id>]`
on both sides.

- `alternative: less` — regression detection.  Fail if current is
  significantly *lower* than baseline.  (Canonical use.)
- `alternative: greater` — cost / latency.  Fail if current is
  significantly *higher* than baseline.
- `alternative: two_sided` — fail on any significant difference.

Defaults: `alpha = 0.05`, `alternative = less`, `min_n = 10`.  The
`min_n` floor is 10 (not 2) because Welch's t-test with df ≈ 1 has
too little power for the verdict to be meaningful; the schema
accepts as low as 2 so test fixtures can opt down, but production
gates should stay at the default or raise it.

The gate emits a detail row carrying `t_stat`, `dof`, `n_current`,
`n_baseline`, `mean_current`, `mean_baseline`, `delta_mean`, and the
p-value, so a reviewer reading the PR comment sees the same numbers
a stats reviewer would ask for.

---

## Per-tag overrides

Different tags can have different floors on the same gate.  Combine
with `aggregation: pass_rate_by_tag`.

```yaml
judge_offline:
  severity: block
  aggregation: pass_rate_by_tag
  threshold:
    min: 0.85                            # average tag pass-rate ≥ 0.85
    per_tag_overrides:
      safety:      1.00                  # zero tolerance on the safety subset
      helpfulness: 0.80                  # adversarial cases get more slack
      sales:       0.90
```

The gate emits one detail row per tag override (`<layer>.tag[<tag>].pass_rate`)
plus the layer's average row, so a failing tag is named in the PR
comment without an external join.

---

## Custom Python check — the escape hatch

When the built-in aggregations don't fit, register a Python function
and EvalGuard treats it as the gate verdict.

```yaml
human:
  severity: log
  custom_check:
    module: my_evals.gates:promote_disagreements
    config:
      destination: ./goldens/candidates.jsonl
```

```python
# my_evals/gates.py
def promote_disagreements(metrics: dict, config: dict) -> dict:
    """Standard signature: (metrics, config) -> {"passed": bool, "details": list}."""
    failing = [r for r in metrics["rows"] if r["passed"] is False]
    write_jsonl(config["destination"], failing)
    return {
        "passed":  True,
        "details": [{"metric": "promoted", "actual": len(failing), "passed": True}],
    }
```

Module path uses dotted or colon-separated form (`my_pkg.gates:fn`
or `my_pkg.gates.fn`).  The function must return a dict with at
least `"passed": bool`; raised exceptions are caught and surfaced as
a failing gate detail (`error: …`) without killing the run.

Each custom-check invocation gets its own `gate.custom_check.invoked`
audit event carrying the module path, the resolved config, the
duration, the verdict, and any exception text — independent
auditability for the escape hatch.

---

## Combining `rules:` with `aggregation/threshold`

A per-layer gate may carry **both** `rules:` (the legacy shape) and
`aggregation/threshold`.  All rules and all aggregation checks must
pass; the gate's `passed` is the logical AND of every detail row.
Useful for "the layer rollup AND a specific evaluator floor AND a
custom Python check" all on the same gate.

---

## `run_mode` — short-circuit policy

How a failing layer interacts with later layers.  Configured at the
top level, not per-gate.

| `run_mode` | Behaviour |
|---|---|
| `short_circuit_blocking_only` *(default)* | A layer's failure skips later layers **only when the failing gate's severity is `block`**.  A `warn`-severity failure runs the rest of the pyramid (the L3 judge still fires even when an L1 heuristic warned). |
| `short_circuit` | Any layer failure (regardless of severity) skips later layers. |
| `always`        | Every configured layer runs regardless of upstream failures.  Useful when you want full per-layer telemetry even on rows that failed early. |

Short-circuited rows emit a `row.short_circuited` event with the
layer index and the reason ("blocking layer failed" or "any layer
failed"), so the audit log shows why later-layer scores are absent.

---

## `gate_strategy` — combining verdicts across trials

A single run can have multiple trials (one per (provider × prompt)
combination).  `gate_strategy` decides how their per-trial verdicts
roll up to the run-level `gate_status`.

| `gate_strategy` | Run passes when… |
|---|---|
| `all` *(default)* | Every trial passes its blocking gates. |
| `any`             | At least one trial passes its blocking gates. |

Useful when you're benchmarking three models and want CI to gate on
"at least one viable candidate" instead of "all three are good".

---

## Where a gate verdict shows up

A single gate failure ripples through every operator surface:

- **CLI exit code** — `0` pass, `2` block-failure, `1` config error.
- **Run record** — `runs.gate_status` ∈ `passed | warned | failed | none`,
  surfaced by `GET /v1/runs` and the run-detail page's header badge.
- **PR comment** — the `evalguard comment` markdown table shows
  per-trial gate verdicts + the Δ-vs-baseline delta when relative
  thresholds were used.  See `packages/action/README.md`.
- **`/reviews`** — failing rows on a run with any blocking-gate
  failure surface in the human-review queue; `/runs/detail` carries
  a "Review N failures →" link when `row_fail_count > 0`.
- **Audit chain** — every gate emits a `gate.evaluated` event
  (severity, aggregation, threshold, baseline diff, custom-check
  block), and `custom_check` invocations get their own
  `gate.custom_check.invoked` event.  Both are part of the per-run
  hash chain (`verify_chain`); a CI gate's pass/fail is
  tamper-evident.

---

## End-to-end example

A realistic blob covering most knobs:

```yaml
version: 1
project: customer-service

providers:
  - id: openai:gpt-4o-mini
    config: { api_key: ${OPENAI_API_KEY}, temperature: 0.2 }

datasets:
  - id: golden
    file: datasets/golden.jsonl

heuristics:
  - { type: json_schema, schema_file: schemas/out.json }
  - { type: length,      max: 600 }
  - { type: not_contains, value: "As an AI" }

metrics:
  - { id: lex.faithfulness,   type: lex.faithfulness,   threshold: 0.3 }
  - { id: lex.context_recall, type: lex.context_recall, threshold: 0.5 }

judges:
  - id: helpfulness
    type: pointwise
    model: openai:gpt-4o
    rubric_file: rubrics/helpfulness.md
    threshold: 4.0

run_mode: short_circuit_blocking_only
gate_strategy: all
cost_cap_usd: 2.00

layers:
  heuristics:
    severity: block
    aggregation: pass_rate
    threshold: { min: 1.0 }       # every row must pass every heuristic

  metrics:
    severity: warn
    evaluator: lex.faithfulness
    threshold:
      type: ttest
      alpha: 0.05
      alternative: less           # warn-only regression detection
      min_n: 30

  judge_offline:
    severity: block
    aggregation: pass_rate_by_tag
    threshold:
      type: relative
      min: 0.90                   # absolute floor
      min_delta_vs_baseline: -0.02
      per_tag_overrides:
        safety:      1.00
        helpfulness: 0.85

  human:
    severity: log
    custom_check:
      module: my_evals.gates:promote_disagreements
      config: { destination: ./goldens/candidates.jsonl }
```

Reading it back: heuristics are a hard wall (every row, every check);
the L2 metric layer warn-detects regressions vs the baseline; L3's
LLM judge has a hard floor with per-tag specifics (zero tolerance on
safety, more slack on adversarial helpfulness); L5 always runs and
quietly promotes failing rows to a candidate-golden file for human
review the next morning.  CI exits non-zero only on L1 or L3 hard
failures; the L2 regression and any L5 promotion are telemetry the
PR comment surfaces but doesn't block on.

---

## Reference

- Code: `packages/cli/evalguard_cli/local/gate.py` (`evaluate_gates`,
  `GateResult`, `_resolve_aggregation`, `_welch_for_gate`,
  `_invoke_custom_check`).
- Schema: `packages/schemas/evalguard.schema.json` (`layers`,
  `gates`, `run_mode`, `gate_strategy`).
- Stats: `packages/cli/evalguard_cli/local/stats.py` (`welchs_t_test`).
- Events: top-level [`README.md` — Event vocabulary](../README.md#event-vocabulary-w3c-prov-activity)
  for the `gate.evaluated` / `gate.custom_check.invoked` /
  `row.short_circuited` events.
- Audit chain — server: [`docs/observability.md` — Audit chain](observability.md#audit-chain--get-v1projectsslugauditevents--auditverify).
- CLI: `evalguard view <run_id>` renders the gate table; `evalguard
  diff <run_a> <run_b>` shows per-metric Δ; `evalguard comment <run_id>
  --baseline f.json` renders the PR-comment markdown.
