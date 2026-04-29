"""Single-process asyncio executor for the evalguard pipeline.

Hierarchy (mirrors the data layer / future API shape):

    run                                  one execution of evalguard.yaml
     └─ trial                            one (provider × prompt) combination
         └─ row                          one dataset row, evaluated through
             └─ score                    each evaluator at each layer

A run with N providers produces N trials. Every trial sees the same
dataset, prompt, evaluators, and gate config; only the model and
provider params differ. Per-trial gate evaluation happens in
``run_cmd``; the executor only writes row + trial results.

Concurrency is bounded per-trial by ``concurrency``. The cost cap is
**global across the run** so a runaway trial doesn't burn budget the
next trial expected to spend.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evalguard_cli.console import console
from evalguard_cli.local.cache import ContentCache
from evalguard_cli.local.gate import LAYER_INDEX
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import LoadedConfig
from evalguard_evaluators import EvalContext, ProviderResult, Score
from evalguard_evaluators.registry import load_evaluator, load_provider


@dataclass
class TrialRecord:
    trial_id: str
    provider_id: str
    provider: str
    model: str
    prompt_id: str | None
    prompt_version_id: str | None
    row_count: int
    row_pass_count: int
    row_fail_count: int
    cost_usd: float
    row_status: str   # "passed" | "failed" | "cost_capped"


@dataclass
class RunRecord:
    run_id: str
    project: str
    config_hash: str
    row_count: int                # total dataset rows × trials
    row_pass_count: int
    row_fail_count: int
    cost_usd: float
    row_status: str               # aggregate across trials
    trials: list[TrialRecord] = field(default_factory=list)


async def execute(
    cfg: LoadedConfig,
    *,
    store: SqliteStore,
    fail_fast: bool = False,
    quiet: bool = False,
) -> RunRecord:
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    project = cfg.project
    store.start_run(run_id, project, cfg.config_hash, assets=cfg.assets)
    if not quiet:
        console.print(
            f"[bold]Run[/bold] {run_id}  project={project}  "
            f"config={cfg.config_hash[:12]}…"
        )

    cache_dir = Path(cfg.raw.get("cache", {}).get("dir", ".evalguard/cache"))
    cache_enabled = bool(cfg.raw.get("cache", {}).get("enabled", True))
    cache = ContentCache(cfg.base_dir / cache_dir, enabled=cache_enabled)

    if not cfg.dataset_rows:
        raise ValueError("at least one dataset is required")
    dataset_id, rows = next(iter(cfg.dataset_rows.items()))

    prompt_id, prompt_template, prompt_version_id = _resolve_prompt(cfg)

    # Evaluator instances are shared across trials. They are
    # configured-once and stateless across rows.
    heuristic_evaluators = _build_evaluators(cfg.raw.get("heuristics", []), default_kind="heuristic")
    metric_evaluators = _build_evaluators(cfg.raw.get("metrics", []), default_kind="metric")
    judge_evaluators = _build_evaluators(cfg.raw.get("judges", []), default_kind="judge")
    evaluators_by_layer: dict[int, list[Any]] = {}
    for ev in heuristic_evaluators + metric_evaluators + judge_evaluators:
        evaluators_by_layer.setdefault(int(ev.layer), []).append(ev)
    layer_order = sorted(evaluators_by_layer)

    run_mode = cfg.raw.get("run_mode", "short_circuit_blocking_only")
    layer_gates = cfg.raw.get("layers") or {}
    blocking_layer_idxs: set[int] = {
        LAYER_INDEX[name]
        for name, gate in layer_gates.items()
        if gate.get("severity", "block") == "block" and name in LAYER_INDEX
    }

    # Run-level shared budget across all trials.
    cost_cap = float(cfg.raw.get("cost_cap_usd", 0)) or None
    total_cost = 0.0
    cost_lock = asyncio.Lock()
    cost_capped = asyncio.Event()
    concurrency = int(cfg.raw.get("concurrency", 8))

    provider_specs = cfg.raw["providers"]
    if not provider_specs:
        raise ValueError("at least one provider is required")

    trials: list[TrialRecord] = []
    started = time.monotonic()

    for provider_spec in provider_specs:
        provider_id = provider_spec["id"]
        provider_cfg = provider_spec.get("config", {})
        provider_name, model = _split_provider_id(provider_id)

        if cost_capped.is_set():
            if not quiet:
                console.print(f"  [yellow]skipping {provider_id} (cost cap reached)[/yellow]")
            continue

        provider = load_provider(provider_name, provider_cfg)
        trial_id = f"trial_{uuid.uuid4().hex[:12]}"
        store.start_trial(
            trial_id, run_id,
            provider_id=provider_id, provider=provider_name, model=model,
            prompt_id=prompt_id, prompt_version_id=prompt_version_id,
            config=provider_cfg,
        )
        if not quiet:
            console.print(f"\n[bold cyan]▶ trial[/bold cyan] {trial_id} · {provider_id}")

        sem = asyncio.Semaphore(concurrency)
        aborted = asyncio.Event()  # per-trial abort (fail_fast or cost cap)

        async def process_row(idx: int, row: dict[str, Any]) -> tuple[float, bool, bool]:
            """Returns (cost, passed, attempted)."""
            nonlocal total_cost
            async with sem:
                if aborted.is_set():
                    return 0.0, False, False

                # Pre-flight cost cap.
                async with cost_lock:
                    if cost_cap is not None and total_cost >= cost_cap:
                        aborted.set()
                        cost_capped.set()
                        return 0.0, False, False

                row_id = str(row.get("id", f"{dataset_id}:{idx}"))
                input_value = row.get("input")
                expected = row.get("expected")
                tags = list(row.get("tags") or [])
                prompt = _render_prompt(prompt_template, row)

                cache_key = ContentCache.key(
                    provider_name, model, prompt, provider_cfg, input_value
                )
                cached = cache.get(cache_key)
                if cached is not None:
                    pr = ProviderResult(
                        output=cached["output"],
                        cost_usd=0.0,
                        latency_ms=cached.get("latency_ms", 0),
                        raw=cached.get("raw", {}),
                    )
                    cache_hit = True
                else:
                    pr = await provider.complete(prompt, model=model)
                    cache.put(cache_key, {
                        "output": pr.output,
                        "latency_ms": pr.latency_ms,
                        "raw": pr.raw,
                    })
                    cache_hit = False

                async with cost_lock:
                    total_cost += pr.cost_usd
                    if cost_cap is not None and total_cost >= cost_cap:
                        aborted.set()
                        cost_capped.set()

                ctx = EvalContext(
                    row_id=row_id, input=input_value, expected=expected,
                    output=pr.output, provider=provider_name, model=model,
                )

                scores: list[Score] = []
                short_circuited_at: int | None = None
                for layer in layer_order:
                    layer_scores: list[Score] = []
                    for ev in evaluators_by_layer[layer]:
                        layer_scores.extend(await ev.evaluate(ctx))
                    scores.extend(layer_scores)
                    layer_failed = any(not s.passed for s in layer_scores)
                    if layer_failed and _should_short_circuit(
                        run_mode, layer, blocking_layer_idxs
                    ):
                        short_circuited_at = layer
                        break

                row_passed = all(s.passed for s in scores) if scores else True
                store.insert_row(
                    run_id, row_id, trial_id=trial_id,
                    input_json=input_value, expected_json=expected,
                    output=pr.output, provider=provider_name, model=model,
                    cost_usd=pr.cost_usd, latency_ms=pr.latency_ms,
                    cache_hit=cache_hit, tags=tags,
                )
                store.insert_scores(
                    run_id, row_id, [_score_to_dict(s) for s in scores],
                    trial_id=trial_id,
                )
                if not quiet:
                    marker = "[green]✓[/green]" if row_passed else "[red]✗[/red]"
                    tail = ""
                    if short_circuited_at is not None:
                        skipped = [str(l) for l in layer_order if l > short_circuited_at]
                        if skipped:
                            tail = (f"  [dim](short-circuit at L{short_circuited_at}; "
                                    f"skipped L{','.join(skipped)})[/dim]")
                    console.print(f"  {marker} {row_id}  ({len(scores)} scores){tail}")
                if fail_fast and not row_passed:
                    aborted.set()
                return pr.cost_usd, row_passed, True

        results = await asyncio.gather(*(process_row(i, r) for i, r in enumerate(rows)))
        attempted = [r for r in results if r[2]]
        trial_pass = sum(1 for _, ok, _ in attempted if ok)
        trial_fail = len(attempted) - trial_pass
        trial_cost = sum(c for c, _, _ in results)

        if cost_capped.is_set() and len(attempted) < len(rows):
            trial_status = "cost_capped"
        elif trial_fail == 0 and len(attempted) == len(rows):
            trial_status = "passed"
        else:
            trial_status = "failed"

        store.record_trial_results(
            trial_id,
            row_status=trial_status,
            cost_usd=trial_cost,
            row_count=len(rows),
            row_pass_count=trial_pass,
            row_fail_count=trial_fail,
        )

        trials.append(TrialRecord(
            trial_id=trial_id,
            provider_id=provider_id,
            provider=provider_name,
            model=model,
            prompt_id=prompt_id,
            prompt_version_id=prompt_version_id,
            row_count=len(rows),
            row_pass_count=trial_pass,
            row_fail_count=trial_fail,
            cost_usd=trial_cost,
            row_status=trial_status,
        ))

        if not quiet:
            console.print(
                f"  [bold]{trial_pass}/{len(rows)}[/bold] rows passed · "
                f"cost=${trial_cost:.4f} · status={trial_status}"
            )

    elapsed = time.monotonic() - started

    # Run-level aggregate
    total_rows = sum(t.row_count for t in trials)
    total_pass = sum(t.row_pass_count for t in trials)
    total_fail = sum(t.row_fail_count for t in trials)

    if cost_capped.is_set():
        row_status = "cost_capped"
    elif any(t.row_status == "failed" for t in trials):
        row_status = "failed"
    elif trials and all(t.row_status == "passed" for t in trials):
        row_status = "passed"
    else:
        row_status = "failed"

    store.record_row_results(
        run_id,
        row_status=row_status,
        cost_usd=total_cost,
        row_count=total_rows,
        row_pass_count=total_pass,
        row_fail_count=total_fail,
    )
    if not quiet:
        console.print(
            f"\n[bold]{total_pass}/{total_rows}[/bold] row-evaluations passed across "
            f"{len(trials)} trial(s) · cost=${total_cost:.4f} · elapsed={elapsed:.2f}s"
            + (" · [yellow]cost-capped[/yellow]" if cost_capped.is_set() else "")
        )

    return RunRecord(
        run_id=run_id, project=project, config_hash=cfg.config_hash,
        row_count=total_rows, row_pass_count=total_pass, row_fail_count=total_fail,
        cost_usd=total_cost, row_status=row_status,
        trials=trials,
    )


# ---------------------------------------------------------------------------


def _resolve_prompt(cfg: LoadedConfig) -> tuple[str | None, str, str | None]:
    """Pick the first prompt (id, body, version_id) or fallback to ``{input}``."""
    prompt_specs = cfg.raw.get("prompts") or []
    if not prompt_specs:
        return None, "{input}", None
    spec = prompt_specs[0]
    pid = spec.get("id")
    body = cfg.prompts.get(pid, "{input}") if pid else "{input}"
    version_id = spec.get("version_id")
    return pid, body, version_id


def _should_short_circuit(run_mode: str, layer: int, blocking_layer_idxs: set[int]) -> bool:
    """Decide whether a failure at ``layer`` should skip later layers.

    - ``always``: never short-circuit — every layer always runs.
    - ``short_circuit``: any layer's failure skips later layers.
    - ``short_circuit_blocking_only``: skip later layers only when the
      failed layer carries a block-severity gate (default).
    """
    if run_mode == "always":
        return False
    if run_mode == "short_circuit":
        return True
    return layer in blocking_layer_idxs


def _build_evaluators(specs: list[dict[str, Any]], default_kind: str) -> list[Any]:
    out = []
    for spec in specs:
        type_name = spec["type"]
        ep_name = f"{default_kind}.{type_name}"
        cfg = {k: v for k, v in spec.items()
               if k not in {"type", "version_id", "schema_version_id", "rubric_version_id"}}
        if "id" not in cfg:
            cfg["id"] = type_name
        out.append(load_evaluator(ep_name, cfg))
    return out


def _split_provider_id(provider_id: str) -> tuple[str, str]:
    if ":" in provider_id:
        a, b = provider_id.split(":", 1)
        return a, b
    return provider_id, "default"


# Identifier-shaped placeholders only: {input}, {question}, {context_1}.
# This deliberately ignores JSON-shaped braces in prompts (e.g. an example
# ``{"summary": "..."}``) so prompts can demonstrate output schemas
# without escaping. Unknown identifiers raise loudly.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _render_prompt(template: str, row: dict[str, Any]) -> str:
    fields: dict[str, Any] = dict(row) if isinstance(row, dict) else {}
    if "input" not in fields:
        fields["input"] = ""
    if isinstance(fields.get("input"), (dict, list)):
        fields["input"] = json.dumps(fields["input"], ensure_ascii=False)

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in fields:
            raise ValueError(
                f"prompt template references missing field {key!r}; "
                f"row keys are {sorted(fields)}"
            )
        value = fields[key]
        return str(value) if value is not None else ""

    return _PLACEHOLDER_RE.sub(_sub, template)


def _score_to_dict(s: Score) -> dict[str, Any]:
    return {
        "evaluator_id": s.evaluator_id,
        "evaluator_kind": s.evaluator_kind,
        "layer": s.layer,
        "value": s.value,
        "passed": s.passed,
        "raw": s.raw,
    }
