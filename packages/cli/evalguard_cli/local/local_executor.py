"""Single-process asyncio executor for the evalguard pipeline.

Pipeline per row:
  prompt_render → provider.complete → heuristics → metrics → judges → persist.
Concurrency is bounded by ``concurrency`` from the config (default 8).
Provider results are content-addressable cached on disk.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from evalguard_cli.local.cache import ContentCache
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import LoadedConfig
from evalguard_evaluators import EvalContext, ProviderResult, Score
from evalguard_evaluators.registry import load_evaluator, load_provider


@dataclass
class RunRecord:
    run_id: str
    project: str
    config_hash: str
    row_count: int
    cost_usd: float
    status: str  # "passed" | "failed"


async def execute(
    cfg: LoadedConfig,
    *,
    store: SqliteStore,
    fail_fast: bool = False,
    quiet: bool = False,
) -> RunRecord:
    console = Console()
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    project = cfg.project
    store.start_run(run_id, project, cfg.config_hash)
    if not quiet:
        console.print(f"[bold]Run[/bold] {run_id}  project={project}  config={cfg.config_hash[:12]}…")

    cache_dir = Path(cfg.raw.get("cache", {}).get("dir", ".evalguard/cache"))
    cache_enabled = bool(cfg.raw.get("cache", {}).get("enabled", True))
    cache = ContentCache(cfg.base_dir / cache_dir, enabled=cache_enabled)

    # Build evaluators once; they're pure data + small refs and safe to share.
    heuristic_evaluators = _build_evaluators(cfg.raw.get("heuristics", []), default_kind="heuristic")
    metric_evaluators = _build_evaluators(cfg.raw.get("metrics", []), default_kind="metric")
    judge_evaluators = _build_evaluators(cfg.raw.get("judges", []), default_kind="judge")

    # Resolve dataset(s). Phase 0 supports the first dataset declared.
    if not cfg.dataset_rows:
        raise ValueError("at least one dataset is required")
    dataset_id, rows = next(iter(cfg.dataset_rows.items()))

    # Resolve prompt: either the first declared prompt's template, or
    # ``{input}`` as a fallback so a config without a prompt still runs.
    prompt_template = next(iter(cfg.prompts.values()), "{input}")

    # Resolve provider list and pick the first; multi-provider matrices
    # are a Phase 1+ concern.
    provider_specs = cfg.raw["providers"]
    provider_id = provider_specs[0]["id"]
    provider_cfg = provider_specs[0].get("config", {})
    provider_name, model = _split_provider_id(provider_id)
    provider = load_provider(provider_name, provider_cfg)

    sem = asyncio.Semaphore(int(cfg.raw.get("concurrency", 8)))
    cost_cap = float(cfg.raw.get("cost_cap_usd", 0)) or None
    total_cost = 0.0
    cost_lock = asyncio.Lock()
    aborted = asyncio.Event()

    async def process_row(idx: int, row: dict[str, Any]) -> tuple[float, bool]:
        nonlocal total_cost
        async with sem:
            if aborted.is_set():
                return 0.0, False
            row_id = str(row.get("id", f"{dataset_id}:{idx}"))
            input_value = row.get("input")
            expected = row.get("expected")
            prompt = _render_prompt(prompt_template, row)

            cache_key = ContentCache.key(provider_name, model, prompt, None, input_value)
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
                if cost_cap is not None and total_cost > cost_cap:
                    aborted.set()

            ctx = EvalContext(
                row_id=row_id,
                input=input_value,
                expected=expected,
                output=pr.output,
                provider=provider_name,
                model=model,
            )

            scores: list[Score] = []
            for ev in heuristic_evaluators + metric_evaluators:
                scores.extend(await ev.evaluate(ctx))
            for ev in judge_evaluators:
                scores.extend(await ev.evaluate(ctx))

            row_passed = all(s.passed for s in scores) if scores else True
            store.insert_row(
                run_id, row_id,
                input_json=input_value,
                expected_json=expected,
                output=pr.output,
                provider=provider_name,
                model=model,
                cost_usd=pr.cost_usd,
                latency_ms=pr.latency_ms,
                cache_hit=cache_hit,
            )
            store.insert_scores(run_id, row_id, [_score_to_dict(s) for s in scores])
            if not quiet:
                marker = "[green]✓[/green]" if row_passed else "[red]✗[/red]"
                console.print(f"  {marker} {row_id}  ({len(scores)} scores)")
            if fail_fast and not row_passed:
                aborted.set()
            return pr.cost_usd, row_passed

    started = time.monotonic()
    results = await asyncio.gather(*(process_row(i, r) for i, r in enumerate(rows)))
    elapsed = time.monotonic() - started

    passed_count = sum(1 for _, ok in results if ok)
    cost_total = sum(c for c, _ in results)
    status = "passed" if passed_count == len(rows) else "failed"
    store.finish_run(run_id, status=status, cost_usd=cost_total, row_count=len(rows))
    if not quiet:
        console.print(
            f"\n[bold]{passed_count}/{len(rows)}[/bold] rows passed · "
            f"cost=${cost_total:.4f} · elapsed={elapsed:.2f}s"
        )
    return RunRecord(
        run_id=run_id,
        project=project,
        config_hash=cfg.config_hash,
        row_count=len(rows),
        cost_usd=cost_total,
        status=status,
    )


def _build_evaluators(specs: list[dict[str, Any]], default_kind: str) -> list[Any]:
    out = []
    for spec in specs:
        type_name = spec["type"]
        # Schema entry-points are namespaced as "<kind>.<type>"
        ep_name = f"{default_kind}.{type_name}"
        cfg = {k: v for k, v in spec.items() if k != "type"}
        if "id" not in cfg:
            cfg["id"] = type_name
        out.append(load_evaluator(ep_name, cfg))
    return out


def _split_provider_id(provider_id: str) -> tuple[str, str]:
    if ":" in provider_id:
        a, b = provider_id.split(":", 1)
        return a, b
    return provider_id, "default"


def _render_prompt(template: str, row: dict[str, Any]) -> str:
    """Best-effort prompt rendering: ``{input}`` substitution + named keys."""
    fields = {**(row if isinstance(row, dict) else {})}
    if "input" not in fields:
        fields["input"] = ""
    if isinstance(fields.get("input"), (dict, list)):
        fields["input"] = json.dumps(fields["input"], ensure_ascii=False)
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        # Missing variables are non-fatal; fall back to the raw template.
        return template


def _score_to_dict(s: Score) -> dict[str, Any]:
    return {
        "evaluator_id": s.evaluator_id,
        "evaluator_kind": s.evaluator_kind,
        "layer": s.layer,
        "value": s.value,
        "passed": s.passed,
        "raw": s.raw,
    }
