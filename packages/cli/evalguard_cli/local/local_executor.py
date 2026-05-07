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
from evalguard_cli.local.audit import (
    AuditHook,
    AuditLog,
    reset_audit_hook,
    set_audit_hook,
)
from evalguard_cli.local.cache import ContentCache
from evalguard_cli.local.gate import LAYER_INDEX
from evalguard_cli.local.retry import (
    ProviderFailed,
    RetryPolicy,
    call_with_retry,
)
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
    audit: "AuditLog | None" = None


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

    audit_cfg = cfg.raw.get("audit") or {}
    audit = AuditLog(
        store, run_id,
        redact_payload=bool(audit_cfg.get("redact_payload", False)),
        project_dir=cfg.base_dir,
    )
    audit.emit(
        "run.started",
        subject_id=project,
        subject_version=cfg.config_hash,
        payload={
            "project":     project,
            "config_hash": cfg.config_hash,
            "asset_count": len(cfg.assets),
            "providers":   [p["id"] for p in cfg.raw.get("providers", [])],
            "run_mode":    cfg.raw.get("run_mode", "short_circuit_blocking_only"),
        },
    )

    # Emit one ``asset.resolved`` event per asset so the provenance graph
    # records exactly which prompt / dataset / rubric / schema / judge /
    # heuristic version_id was used. ``cfg.assets`` is already content-
    # hashed by the YAML loader; this just promotes that snapshot into
    # the event stream so downstream consumers (PR comment, lineage UI,
    # compliance archive) can follow asset → run → trial → score links.
    for asset in cfg.assets:
        audit.emit(
            "asset.resolved",
            subject_kind=asset.kind,
            subject_id=asset.asset_id,
            subject_version=asset.version_id,
            outputs={"version_id": asset.version_id},
            payload={
                "kind":       asset.kind,
                "asset_id":   asset.asset_id,
                "version_id": asset.version_id,
                "source":     asset.source,
            },
        )

    if not quiet:
        console.print(
            f"[bold]Run[/bold] {run_id}  project={project}  "
            f"config={cfg.config_hash[:12]}…  actor={audit.actor.actor_id}"
        )

    cache_dir = Path(cfg.raw.get("cache", {}).get("dir", ".evalguard/cache"))
    cache_enabled = bool(cfg.raw.get("cache", {}).get("enabled", True))
    cache = ContentCache(cfg.base_dir / cache_dir, enabled=cache_enabled)

    if not cfg.dataset_rows:
        raise ValueError("at least one dataset is required")
    dataset_id, rows = next(iter(cfg.dataset_rows.items()))

    prompt_id, prompt_template, prompt_version_id = _resolve_prompt(cfg)

    # Evaluator instances + their resolved specs are paired so every
    # invocation event can carry the full evaluator config (model,
    # threshold, rubric_version_id, etc.) — not just the resolved
    # scores. Specs are post-${ENV} and post-resource-inlining.
    heuristic_evaluators = _build_evaluators(cfg.raw.get("heuristics", []), default_kind="heuristic")
    metric_evaluators = _build_evaluators(cfg.raw.get("metrics", []), default_kind="metric")
    judge_evaluators = _build_evaluators(cfg.raw.get("judges", []), default_kind="judge")
    evaluators_by_layer: dict[int, list[tuple[Any, dict[str, Any]]]] = {}
    for ev, spec in heuristic_evaluators + metric_evaluators + judge_evaluators:
        evaluators_by_layer.setdefault(int(ev.layer), []).append((ev, spec))
    layer_order = sorted(evaluators_by_layer)

    run_mode = cfg.raw.get("run_mode", "short_circuit_blocking_only")
    layer_gates = cfg.raw.get("layers") or {}
    blocking_layer_idxs: set[int] = {
        LAYER_INDEX[name]
        for name, gate in layer_gates.items()
        if gate.get("severity", "block") == "block" and name in LAYER_INDEX
    }

    # Run-wide default retry policy; per-provider ``config.retry``
    # overrides it inside the trial loop.
    run_retry_cfg = cfg.raw.get("retry") or {}

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
        raw_provider_cfg = provider_spec.get("config", {})
        # Operational keys (retry, etc.) are stripped from the SDK-bound
        # config so they don't leak into provider.complete() kwargs and
        # so cache-key stability is preserved when users tune them.
        provider_cfg = {k: v for k, v in raw_provider_cfg.items() if k not in {"retry"}}
        provider_retry_cfg = raw_provider_cfg.get("retry")
        provider_name, model = _split_provider_id(provider_id)

        if cost_capped.is_set():
            if not quiet:
                console.print(f"  [yellow]skipping {provider_id} (cost cap reached)[/yellow]")
            continue

        provider = load_provider(provider_name, provider_cfg)
        # Retry policy: per-provider ``config.retry`` wins over the
        # run-level ``retry:`` block. Both are optional; either being
        # absent falls back to ``RetryPolicy``'s defaults.
        trial_retry_policy = RetryPolicy.from_config(
            provider_retry_cfg or run_retry_cfg
        )
        trial_id = f"trial_{uuid.uuid4().hex[:12]}"
        # ``raw_provider_cfg`` (with operational keys like ``retry``)
        # goes to the trial record and the audit event so an auditor
        # can reconstruct the FULL criteria the user set — not just
        # the SDK-bound subset. ``provider_cfg`` (stripped) is what
        # actually drove ``load_provider`` and the cache key.
        store.start_trial(
            trial_id, run_id,
            provider_id=provider_id, provider=provider_name, model=model,
            prompt_id=prompt_id, prompt_version_id=prompt_version_id,
            config=raw_provider_cfg,
        )
        audit.emit(
            "trial.started",
            trial_id=trial_id,
            subject_id=provider_id,
            payload={
                "provider":          provider_name,
                "model":             model,
                "provider_config":   raw_provider_cfg,
                "retry":             provider_retry_cfg or run_retry_cfg or None,
                "prompt_id":         prompt_id,
                "prompt_version_id": prompt_version_id,
            },
        )
        if not quiet:
            console.print(f"\n[bold cyan]▶ trial[/bold cyan] {trial_id} · {provider_id}")

        sem = asyncio.Semaphore(concurrency)
        aborted = asyncio.Event()  # per-trial abort (fail_fast or cost cap)

        # Per-trial provider-instance cache for per-row overrides. Without
        # it, ``concurrency=8 × 1000 rows`` paid the
        # ``importlib.metadata.entry_points`` walk inside ``load_provider``
        # 1000 times. Keyed by (provider_name, frozen_cfg) so two rows
        # with the same effective config share one provider instance.
        provider_cache: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] = {}

        def _provider_key(name: str, cfg: dict[str, Any]) -> tuple[str, tuple[tuple[str, Any], ...]]:
            # Sort keys so insertion order doesn't perturb the cache. Use
            # ``repr`` for non-hashable values (nested dict/list) — slow
            # path but only on cache miss.
            try:
                items = tuple(sorted(cfg.items()))
                hash(items)
            except TypeError:
                items = tuple(sorted((k, repr(v)) for k, v in cfg.items()))
            return (name, items)

        def _get_or_load_provider(name: str, cfg: dict[str, Any]) -> Any:
            key = _provider_key(name, cfg)
            inst = provider_cache.get(key)
            if inst is None:
                inst = load_provider(name, cfg)
                provider_cache[key] = inst
            return inst

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

                # Per-row provider/params override. ``row.provider`` (e.g.
                # "openai:gpt-4o") swaps the model used for this row's
                # completion; ``row.params`` is shallow-merged onto the
                # trial's provider config (row wins). Cache key reflects
                # the *effective* (provider, model, params) so two rows
                # with different overrides don't collide.
                row_override = row.get("provider")
                if row_override:
                    eff_provider_name, eff_model = _split_provider_id(row_override)
                else:
                    eff_provider_name, eff_model = provider_name, model
                # ``row.params`` is reserved for SDK-bound provider
                # config (temperature, output, max_tokens, …) and is
                # shallow-merged onto the trial provider's stripped
                # config. ``row.retry`` (operational) is read separately
                # below so it never leaks into ``provider.complete``.
                row_params = row.get("params") or {}
                # Hoist ``row_retry_cfg`` out of the cache-miss branch
                # so audit payloads that reference ``row_override``
                # work even when the row was served from cache.
                row_retry_cfg = row.get("retry") if isinstance(row, dict) else None
                eff_provider_cfg = {**provider_cfg, **row_params} if row_params else provider_cfg
                if row_override and eff_provider_name != provider_name:
                    eff_provider = _get_or_load_provider(eff_provider_name, eff_provider_cfg)
                elif row_params:
                    # Same provider class, different params → fetch
                    # (or instantiate-and-cache) a provider with the
                    # merged config. Repeated rows with identical
                    # overrides share one instance.
                    eff_provider = _get_or_load_provider(eff_provider_name, eff_provider_cfg)
                else:
                    eff_provider = provider
                effective_provider_name, effective_model = eff_provider_name, eff_model

                cache_key = ContentCache.key(
                    eff_provider_name, eff_model, prompt, eff_provider_cfg, input_value
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
                    # Per-trial retry policy with optional per-row override
                    # via top-level ``row.retry``. ``row_retry_cfg`` is
                    # already hoisted above so the cache-hit path can
                    # also include it in audit payloads.
                    eff_policy = (
                        RetryPolicy.from_config(row_retry_cfg) if row_retry_cfg
                        else trial_retry_policy
                    )

                    def _on_retry(attempt: int, exc: BaseException, delay_ms: int) -> None:
                        audit.emit(
                            "provider.retry",
                            trial_id=trial_id, row_id=row_id,
                            subject_id=f"{eff_provider_name}:{eff_model}",
                            payload={
                                "provider":      eff_provider_name,
                                "model":         eff_model,
                                "attempt":       attempt,
                                "max_retries":   eff_policy.max_retries,
                                "delay_ms":      delay_ms,
                                "error_type":    type(exc).__name__,
                                "error":         str(exc)[:240],
                                "cache_key":     cache_key,
                            },
                        )

                    try:
                        pr = await call_with_retry(
                            coro_factory=lambda: eff_provider.complete(prompt, model=eff_model),
                            policy=eff_policy,
                            on_retry=_on_retry,
                            # Pass the trial-abort event so a 429
                            # storm aborts as soon as another row's
                            # post-call cost increment trips the cap,
                            # rather than burning the full retry
                            # budget under back-pressure.
                            cancel=aborted,
                        )
                    except ProviderFailed as fail:
                        # Attribute partial cost the provider billed
                        # across attempts to the run total — never
                        # silently lose it. The pre-flight check on
                        # the next row will see the updated total.
                        if fail.total_cost_usd:
                            async with cost_lock:
                                total_cost += fail.total_cost_usd
                                if cost_cap is not None and total_cost >= cost_cap:
                                    aborted.set()
                                    cost_capped.set()
                        # If this failure was a retry-loop cancel
                        # (cost cap fired elsewhere mid-backoff), we
                        # skip the row cleanly — no provider.failed
                        # event, no row insert. The cap-emitting row
                        # handled the bookkeeping.
                        if fail.cancelled:
                            return fail.total_cost_usd, False, False
                        # Final failure: emit ``provider.failed``, mark the
                        # row as evaluator-failed (no scores), and return
                        # without raising so the rest of the trial proceeds.
                        audit.emit(
                            "provider.failed",
                            trial_id=trial_id, row_id=row_id,
                            subject_id=f"{eff_provider_name}:{eff_model}",
                            payload={
                                "provider":     eff_provider_name,
                                "model":        eff_model,
                                "attempts":     fail.attempts,
                                "n_attempts":   len(fail.attempts),
                                "error_type":   type(fail.cause).__name__,
                                "error":        str(fail.cause)[:240],
                                "trial_provider_id": provider_id,
                                "row_override": bool(row_override) or bool(row_params) or bool(row_retry_cfg),
                            },
                        )
                        store.insert_row(
                            run_id, row_id, trial_id=trial_id,
                            input_json=input_value, expected_json=expected,
                            output="", provider=eff_provider_name, model=eff_model,
                            cost_usd=0.0, latency_ms=0,
                            cache_hit=False, tags=tags,
                        )
                        if not quiet:
                            console.print(
                                f"  [red]✗[/red] {row_id}  "
                                f"[dim](provider failed after "
                                f"{len(fail.attempts)} attempt(s): "
                                f"{type(fail.cause).__name__})[/dim]"
                            )
                        if fail_fast:
                            aborted.set()
                        return 0.0, False, True
                    cache.put(cache_key, {
                        "output": pr.output,
                        "latency_ms": pr.latency_ms,
                        "raw": pr.raw,
                    })
                    cache_hit = False

                async with cost_lock:
                    total_cost += pr.cost_usd
                    just_capped = (
                        cost_cap is not None
                        and total_cost >= cost_cap
                        and not cost_capped.is_set()
                    )
                    if cost_cap is not None and total_cost >= cost_cap:
                        aborted.set()
                        cost_capped.set()
                if just_capped:
                    audit.emit(
                        "run.cost_capped",
                        trial_id=trial_id, row_id=row_id,
                        subject_id=cfg.project,
                        payload={
                            "cost_cap_usd":   cost_cap,
                            "total_cost_usd": total_cost,
                            "trial_id":       trial_id,
                            "row_id":         row_id,
                            "reason": "post-call cap reached; subsequent rows will abort pre-flight",
                        },
                        cost_usd=total_cost,
                    )

                # ``provider``/``model``/``model_params`` reflect what
                # actually drove the call (post-row-override) so an
                # auditor can reconstruct stratified runs row-by-row.
                # ``trial_provider_id`` preserves the trial-level
                # configured provider for grouping.
                audit.emit(
                    "provider.called",
                    trial_id=trial_id, row_id=row_id,
                    subject_id=f"{eff_provider_name}:{eff_model}" if row_override or row_params
                                else provider_id,
                    inputs=prompt,
                    outputs=pr.output,
                    payload={
                        "provider":         eff_provider_name,
                        "model":            eff_model,
                        "model_params":     eff_provider_cfg,
                        "trial_provider_id": provider_id,
                        "row_override":     bool(row_override) or bool(row_params) or bool(row_retry_cfg),
                        "rendered_prompt":  prompt,
                        "raw_response":     pr.output,
                        "raw_provider":     pr.raw,
                        "cache_hit":        cache_hit,
                        "tokens":           _extract_tokens(pr.raw),
                    },
                    cost_usd=pr.cost_usd,
                    duration_ms=pr.latency_ms,
                )

                # ``extra`` exposes the full row to evaluators so RAG /
                # text_to_sql / agent rows can carry their own fields
                # (``contexts``, ``question``, ``schema_ref``, …) without
                # the executor needing to know about every domain shape.
                ctx = EvalContext(
                    row_id=row_id, input=input_value, expected=expected,
                    output=pr.output, provider=effective_provider_name, model=effective_model,
                    extra=dict(row) if isinstance(row, dict) else {},
                )

                scores: list[Score] = []
                short_circuited_at: int | None = None
                for layer in layer_order:
                    layer_scores: list[Score] = []
                    for ev, ev_spec in evaluators_by_layer[layer]:
                        # Pre-allocate the evaluator-event's span so the
                        # evaluator can emit nested ``provider.called``
                        # events (e.g. a judge's own LLM call) with the
                        # right parent_span_id before this event itself
                        # is emitted.
                        #
                        # The hook is stored task-local via a ContextVar
                        # rather than on the evaluator instance, because
                        # evaluator instances are shared across rows and
                        # asyncio.gather would otherwise let one row's
                        # hook leak into another row's evaluate() call.
                        ev_span_id = uuid.uuid4().hex[:16]
                        hook = AuditHook(
                            audit, ev_span_id,
                            trial_id=trial_id, row_id=row_id,
                        )
                        token = set_audit_hook(hook)
                        ev_t0 = time.monotonic()
                        try:
                            ev_scores = await ev.evaluate(ctx)
                        finally:
                            reset_audit_hook(token)
                        ev_dur = int((time.monotonic() - ev_t0) * 1000)
                        layer_scores.extend(ev_scores)
                        # Score.evaluator_id is the user-configured id (e.g.
                        # "helpfulness_v3"); fall back to the spec id when the
                        # evaluator emitted no scores.
                        first = ev_scores[0] if ev_scores else None
                        ev_kind = (first.evaluator_kind if first
                                    else ev_spec.get("kind", "heuristic"))
                        ev_id = (first.evaluator_id if first
                                  else ev_spec.get("id", ev_spec.get("type", type(ev).__name__)))
                        ev_version = ev_spec.get("version_id")
                        kind_event = f"evaluator.{ev_kind}.invoked"
                        if kind_event not in {"evaluator.heuristic.invoked",
                                               "evaluator.metric.invoked",
                                               "evaluator.judge.invoked"}:
                            raise ValueError(
                                f"unknown evaluator kind {ev_kind!r} for "
                                f"{ev_id!r}; expected heuristic / metric / judge. "
                                "Add the new kind to EVENT_KINDS and to "
                                "evalguard.run.schema.json#/$defs/event.kind."
                            )
                        audit.emit(
                            kind_event,
                            trial_id=trial_id, row_id=row_id,
                            span_id=ev_span_id,
                            subject_kind=ev_kind,
                            subject_id=ev_id,
                            subject_version=ev_version,
                            inputs=pr.output,
                            outputs=[s.value for s in ev_scores],
                            payload={
                                # What ran
                                "evaluator_id":   ev_id,
                                "evaluator_kind": ev_kind,
                                "evaluator_type": ev_spec.get("type"),
                                "layer":          layer,
                                # Configuration as set by the user — captures
                                # the full pass/fail criteria for this run:
                                # threshold, model, model_params, rubric, etc.
                                "spec":               ev_spec,
                                "threshold":          ev_spec.get("threshold"),
                                "model":              ev_spec.get("model"),
                                "model_params":       ev_spec.get("params"),
                                "rubric_version_id":  ev_spec.get("rubric_version_id"),
                                "schema_version_id":  ev_spec.get("schema_version_id"),
                                # What it produced
                                "scores": [
                                    {"value": s.value, "passed": s.passed,
                                     "raw": s.raw}
                                    for s in ev_scores
                                ],
                            },
                            duration_ms=ev_dur,
                        )
                    scores.extend(layer_scores)
                    layer_failed = any(not s.passed for s in layer_scores)
                    if layer_failed and _should_short_circuit(
                        run_mode, layer, blocking_layer_idxs
                    ):
                        short_circuited_at = layer
                        skipped = [l for l in layer_order if l > layer]
                        audit.emit(
                            "row.short_circuited",
                            trial_id=trial_id, row_id=row_id,
                            subject_kind="row", subject_id=row_id,
                            payload={
                                "failed_at_layer": layer,
                                "skipped_layers":  skipped,
                                "run_mode":        run_mode,
                                "reason": (
                                    "blocking_severity_layer_failed"
                                    if run_mode == "short_circuit_blocking_only"
                                    else "any_layer_failed"
                                ),
                            },
                        )
                        break

                row_passed = all(s.passed for s in scores) if scores else True
                store.insert_row(
                    run_id, row_id, trial_id=trial_id,
                    input_json=input_value, expected_json=expected,
                    output=pr.output, provider=eff_provider_name, model=eff_model,
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
        audit.emit(
            "trial.finalized",
            trial_id=trial_id,
            subject_id=provider_id,
            payload={
                "row_count":      len(rows),
                "row_pass_count": trial_pass,
                "row_fail_count": trial_fail,
                "row_status":     trial_status,
                "cost_usd":       trial_cost,
            },
            cost_usd=trial_cost,
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
        trials=trials, audit=audit,
    )


def _extract_tokens(raw: dict[str, Any] | None) -> dict[str, int] | None:
    """Best-effort token extraction from common provider raw shapes."""
    if not raw:
        return None
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if not isinstance(usage, dict):
        return None
    return {
        "prompt":     int(usage.get("prompt_tokens", 0) or 0),
        "completion": int(usage.get("completion_tokens", 0) or 0),
        "total":      int(usage.get("total_tokens", 0) or 0),
    }


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


def _build_evaluators(
    specs: list[dict[str, Any]], default_kind: str,
) -> list[tuple[Any, dict[str, Any]]]:
    """Return (instance, resolved_spec) pairs.

    The returned spec is the one the audit log will record on every
    invocation; it includes ``version_id`` (content hash of the asset)
    and any inlined assets like ``schema`` / ``rubric`` resolved by the
    YAML loader, but excludes the synthesized ``id`` fallback so the
    audit reflects what the user actually configured.
    """
    out: list[tuple[Any, dict[str, Any]]] = []
    for spec in specs:
        type_name = spec["type"]
        ep_name = f"{default_kind}.{type_name}"
        cfg = {k: v for k, v in spec.items()
               if k not in {"type", "version_id", "schema_version_id", "rubric_version_id"}}
        if "id" not in cfg:
            cfg["id"] = type_name
        instance = load_evaluator(ep_name, cfg)
        # Carry the resolved-but-unhashed spec for audit. We add ``kind``
        # so the caller can look it up without an isinstance check.
        spec_for_audit = {**spec, "kind": default_kind}
        out.append((instance, spec_for_audit))
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

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in fields:
            raise ValueError(
                f"prompt template references missing field {key!r}; "
                f"row keys are {sorted(fields)}"
            )
        value = fields[key]
        if value is None:
            return ""
        # JSON-serialize structured values so RAG ``{contexts}`` (a list
        # of strings) and SQL ``{schema_ref}`` (a dict) render cleanly
        # rather than leaking Python repr (``['c1', 'c2']``).
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

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
