"""``POST /v1/projects/{slug}/invoke`` — Phase PROXY-2 production gateway.

The endpoint that turns EvalGuard from "track" to "be the gateway":
the production service points its LLM URL at EvalGuard, every call is
scored against the project's stored ``evalguard.yaml``, and every
call shows up in ``/calls/`` for triage with the same drill-down the
batch-eval rows already use.

Request shape (``InvokeRequest``):

    {
      "input":    <str | dict>,   # the prompt / context the model sees
      "expected": <any | null>,   # the reference answer (optional)
      "tags":     [<str>, ...],   # operator-defined row tags
      "extra":    {<str>: <any>}, # passed through to evaluator ctx.extra
      "row_id":   <str | null>    # caller-supplied id; defaults to a uuid
    }

Response shape (``InvokeResponse``):

    {
      "output":     <str>,
      "passed":     <bool>,
      "cost_usd":   <float>,
      "latency_ms": <int>,
      "run_id":     <str>,        # the day's live run
      "trial_id":   <str>,        # the provider+model trial under that run
      "row_id":     <str>,
      "scores":     [<Score>, ...]
    }

Failure modes (recorded as rows even when 502):

- Provider raised (network, 429, 5xx) → row written with ``passed=false``,
  ``error`` populated, response is **502** with the same body shape.
- Config missing / unparseable / no providers → 422 (no row written —
  the call never reached the model).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.engine import Connection


logger = logging.getLogger("evalguard.api.invoke")

from evalguard_api.audit_persistence import emit_event
from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import apply_rls_context, resolve_project_or_404
from evalguard_api.live import (
    LiveCallRecord, ensure_live_run, ensure_live_trial,
    parse_provider_id, record_call,
)
from evalguard_api.models import InvokeRequest, InvokeResponse
from evalguard_api.quotas import (
    DEFAULT_RATE_LIMIT_PER_MINUTE, rate_limit_check, todays_live_run_cost,
)


router = APIRouter()


# Hard ceiling on one proxied call's provider latency.  An upstream
# LLM that hangs forever would otherwise pin a worker; 60s is generous
# for a single completion while still catching real outages.  A future
# slice can make this per-project via the stored config.
_PROVIDER_CALL_TIMEOUT_S: float = 60.0

# Cap on the provider-exception message head we capture into
# ``payload.error`` (round-5 ultra-review, Security B).  Provider
# SDKs frequently echo the user's prompt into error messages; the
# audit chain is immutable + project-readable so an unbounded
# capture would leak production PII to every project member.  240
# chars matches ``output_preview`` and is enough to triage class
# + leading words; full traces stay in the structured access log.
_ERROR_MSG_CHARS: int = 240


# Project-resolution + cross-org 404 — see ``db.py:resolve_project_or_404``.
_resolve_project = resolve_project_or_404


def _load_latest_config(conn: Connection, project_id: str) -> dict:
    """Fetch + parse the project's latest stored config.  422s if no
    config has been pushed or the bytes don't parse as YAML — there's
    nothing to invoke against.

    PROXY-2.5 review-pass D4: parsing happens once per content hash,
    not once per call.  A high-traffic project (100 req/s) was
    re-parsing the same YAML 100× per second; the in-process cache
    keyed on ``content_sha256`` cuts that to 1 parse per push.  The
    cache invalidates naturally when ``evalguard push-config`` lands
    a new revision (different hash → cache miss → re-parse).
    """
    row = conn.execute(
        text("""SELECT content_sha256, content
                FROM project_configs
                WHERE project_id = :pid
                ORDER BY pushed_at DESC, id DESC
                LIMIT 1"""),
        {"pid": project_id},
    ).mappings().fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No project config pushed.  Run `evalguard push-config` first.",
        )
    cached = _parse_config_cached(row["content_sha256"], row["content"])
    return cached


# Bounded LRU keyed on ``content_sha256``.  64 entries is plenty —
# typical deployments serve a handful of projects, each with maybe
# a few config revisions in active use; a project with hundreds of
# distinct configs would still keep its hottest in cache.  Memory
# bound: ~64 × max-config-size ≈ 32 MiB worst-case (configs are
# capped at 512 KiB at push time).
@functools.lru_cache(maxsize=64)
def _parse_config_cached(content_sha256: str, content: str) -> dict:
    """Hashing the content as the cache key means a push of new
    bytes (different SHA) cleanly invalidates the entry; we don't
    have to remember to clear anything on a push.  Returns the
    parsed dict; raises a 422 on malformed YAML."""
    try:
        cfg = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Stored config is not valid YAML: {e}",
        ) from None
    if not isinstance(cfg, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Stored config must be a YAML mapping at the top level.",
        )
    return cfg


@router.post(
    "/v1/projects/{project_slug}/invoke",
    response_model=InvokeResponse,
    tags=["proxy"],
)
async def invoke(
    project_slug: str,
    body: InvokeRequest,
    response: Response,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> InvokeResponse:
    """Forward one call to the project's configured provider, score
    it, record the row under today's live run, and return the LLM
    output synchronously.

    The first provider listed in the stored config drives the call;
    multi-provider routing is a separate slice (the proxy is the
    gateway, not the A/B harness).

    **Connection lifecycle** — round-4 review-pass #6.  Earlier the
    handler took ``conn: Connection = Depends(get_conn)``, which
    held a pool connection open for the entire request (including
    the up-to-60s provider call).  Under modest load (10 concurrent
    slow providers, default ``pool_size=10``) that exhausted the
    pool and stalled cheap reads like ``/v1/health``.  Now we
    explicitly open two SHORT transactions — one to resolve the
    project + load the config, one to write the row — and the
    provider call runs in between holding no DB connection.  Auth
    (``require_principal``) already opens + closes its own tx, so
    we don't pay a fourth conn for it.
    """
    engine = request.app.state.engine

    # --- Phase 1: resolve project + load config + check cost cap ---
    #
    # Short transaction.  Folds three reads into one round-trip so the
    # provider call can run in Phase 2 with no DB conn held (the
    # whole point of the round-4 #6 refactor).  Cost-cap check lives
    # here so we never start a provider call we'd refuse to record;
    # rate-limit lives BELOW (memory-only, no DB needed).
    with engine.begin() as conn:
        apply_rls_context(
            conn, org_id=principal.org_id, is_admin=principal.is_admin,
        )
        project = _resolve_project(conn, principal, project_slug)
        project_id   = project["project_id"]
        project_name = project["name"]
        cfg = _load_latest_config(conn, project_id)
        # Read today's accumulated cost while the conn is still open;
        # the cost-cap branch below compares it without a second
        # round-trip.
        today_cost_usd = todays_live_run_cost(conn, project_id)
    # ``conn`` is returned to the pool here.

    # --- Quota gate: per-key rate limit (round-4 big-ticket #7) ---
    #
    # Cheap in-memory sliding window per ``key_id``.  Refuses the
    # provider call (and explicitly does NOT persist a row — under
    # sustained abuse a hammering client would flood ``run_rows`` and
    # the operator's /calls/ view).  The structured access-log line
    # the middleware emits captures the rejection for audit.
    rate_limit_rpm = int(cfg.get("rate_limit_per_minute") or DEFAULT_RATE_LIMIT_PER_MINUTE)
    allowed, retry_after_s = rate_limit_check(
        principal.key_id, limit_per_minute=rate_limit_rpm,
    )
    if not allowed:
        # ``Retry-After`` is the time until the OLDEST in-window
        # timestamp ages out — dynamic so a project with a tight
        # cap (e.g. 5/min) gets a small Retry-After rather than the
        # fixed 60s that misled callers under the previous revision.
        #
        # Audit asymmetry (round-5 ultra-review, Security L): rate-
        # limit refusals deliberately do NOT emit chain events.
        # Two reasons: (a) under sustained abuse a hammering client
        # would flood the audit chain at line rate (cost-cap
        # refusals are rare-and-important; rate-limit refusals are
        # noise-under-attack), and (b) the structured access-log
        # middleware already captures every 429 with key_id +
        # endpoint + status, which is the right surface for "who
        # got throttled".  ``/audit/events`` covers "what calls did
        # EvalGuard process"; the access log covers "what HTTP
        # requests did EvalGuard receive".  Different questions,
        # different surfaces.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded: {rate_limit_rpm} requests/minute per API key. "
                f"Tune ``rate_limit_per_minute`` in the project config to raise the cap, "
                f"or back off and retry."
            ),
            headers={"Retry-After": str(retry_after_s)},
        )

    # --- Quota gate: per-project daily cost cap ---
    #
    # Compared against today's accumulated ``runs.cost_usd`` which
    # the proxy increments atomically inside ``record_call`` (PROXY-2).
    # 402 Payment Required is the semantically correct status; the
    # row IS persisted (Phase 3 below) so the operator sees the
    # rejection in /calls/?tab=failures with the cap-exceeded
    # reason — cost-cap events are rare and important, unlike rate-
    # limit events which would flood the table.
    cost_cap_usd = float(cfg.get("cost_cap_usd_daily") or 0.0)
    cost_cap_exceeded = cost_cap_usd > 0 and today_cost_usd >= cost_cap_usd

    providers = cfg.get("providers") or []
    if not providers:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Stored config has no ``providers:`` entry.",
        )
    provider_spec = providers[0]
    provider_id   = provider_spec.get("id")
    if not isinstance(provider_id, str) or not provider_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="First provider entry has no string ``id``.",
        )
    provider_name, model = parse_provider_id(provider_id)
    provider_cfg = provider_spec.get("config") or {}

    # Deferred to call time so a misconfigured venv (missing
    # ``evalguard-evaluators`` dep, broken entry-point registration)
    # surfaces as a 422 on /invoke rather than killing the whole
    # server at startup.  The dep IS hard-required now (PROXY-2
    # added it to ``apps/api/pyproject.toml``); the laziness is
    # purely a graceful-degradation choice.
    from evalguard_evaluators.base import EvalContext
    from evalguard_evaluators.registry import load_evaluator, load_provider

    try:
        provider = load_provider(provider_name, provider_cfg)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to load provider {provider_id!r}: {e}",
        ) from e

    # Coerce ``input`` to a prompt string.  Dicts go through
    # ``json.dumps`` so the model sees a stable rendering instead of
    # ``str(dict)`` (Python's repr is not what callers want).
    prompt = body.input if isinstance(body.input, str) else _to_prompt(body.input)
    row_id = body.row_id or f"r-{uuid.uuid4().hex[:12]}"

    # --- provider call --------------------------------------------------------
    # Round-4 review-pass: differentiate upstream throttling from
    # generic upstream errors so the caller's retry logic does the
    # right thing.  ``True`` here forces a 429 + Retry-After in the
    # response below; otherwise a generic 502 fires.  Detection is
    # by string match against the exception message — providers
    # vary in exception class names (httpx.HTTPStatusError,
    # openai.RateLimitError, anthropic.RateLimitError, …) so a
    # text-based test is the portable choice.
    is_rate_limited = False
    # Cost-cap short-circuit (round-4 big-ticket #7).  When today's
    # accumulated cost already meets / exceeds the cap we pre-set
    # ``error`` so the provider-call block below skips cleanly;
    # the row still gets persisted (Phase 3) so /calls/ shows the
    # rejection.  Returns 402 Payment Required via the status-code
    # branch at the end of the handler.
    is_cost_capped = cost_cap_exceeded
    error: str | None = (
        f"cost_cap_exceeded: today's spend ${today_cost_usd:.4f} "
        f"meets/exceeds project daily cap ${cost_cap_usd:.2f}"
        if is_cost_capped else None
    )
    output = ""
    cost_usd = 0.0
    latency_ms = 0
    t0 = time.monotonic()
    if error is None:
        try:
            # ``asyncio.wait_for`` so a hung provider can't pin the worker
            # forever — the proxy is on the production hot path and
            # surviving upstream outages is part of the contract.  On
            # timeout we still persist the row (under the failures tab)
            # so the operator sees the regression in /calls/.
            result = await asyncio.wait_for(
                provider.complete(prompt, model=model),
                timeout=_PROVIDER_CALL_TIMEOUT_S,
            )
            output     = result.output
            cost_usd   = float(result.cost_usd)
            latency_ms = int(result.latency_ms)
        except asyncio.TimeoutError:
            error = (
                f"TimeoutError: provider {provider_id!r} did not respond "
                f"within {_PROVIDER_CALL_TIMEOUT_S:.0f}s"
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
        except Exception as e:
            # Round-5 ultra-review (Security B): provider exception
            # messages often echo the request body — OpenAI 400s
            # include the offending prompt, Anthropic errors quote
            # input, etc.  That string lands verbatim in
            # ``payload.error`` inside ``event_json`` which is
            # immutable + project-readable.  Cap at 240 chars (same
            # length as ``output_preview``) + keep only the
            # exception class name + the head of the message so we
            # have enough triage signal without writing the
            # operator's PII into the audit chain.
            raw_msg = str(e)
            if len(raw_msg) > _ERROR_MSG_CHARS:
                raw_msg = raw_msg[:_ERROR_MSG_CHARS] + "…[truncated]"
            error = f"{type(e).__name__}: {raw_msg}"
            latency_ms = int((time.monotonic() - t0) * 1000)
            is_rate_limited = _looks_like_rate_limit(e)

    # --- evaluators -----------------------------------------------------------
    scores_payload: list[dict[str, Any]] = []
    passed = error is None  # default-pass for vacuous configs; default-fail on provider error
    if error is None:
        ctx = EvalContext(
            row_id=row_id, input=body.input, expected=body.expected,
            output=output, provider=provider_name, model=model,
            extra=body.extra or {},
        )
        # Mirror ``local_executor._build_evaluators``: the YAML keys
        # ``heuristics:`` / ``metrics:`` / ``judges:`` map to entry-
        # point name prefixes ``heuristic.`` / ``metric.`` / ``judge.``
        # so a YAML spec ``- type: length`` under ``heuristics:`` loads
        # the ``heuristic.length`` evaluator.  Without the prefix the
        # registry lookup fails and we'd silently pass every call.
        ev_specs: list[tuple[str, dict]] = []
        for yaml_key, ep_kind in (("heuristics", "heuristic"),
                                   ("metrics", "metric"),
                                   ("judges", "judge")):
            for spec in cfg.get(yaml_key) or []:
                if isinstance(spec, dict) and isinstance(spec.get("type"), str):
                    ev_specs.append((ep_kind, spec))

        all_scores = []
        for ep_kind, spec in ev_specs:
            ev_type = spec["type"]
            ep_name = f"{ep_kind}.{ev_type}"
            ev_cfg = {k: v for k, v in spec.items()
                      if k not in {"type", "version_id",
                                   "schema_version_id", "rubric_version_id"}}
            ev_cfg.setdefault("id", ev_type)
            try:
                ev = load_evaluator(ep_name, ev_cfg)
                ev_scores = await ev.evaluate(ctx)
            except Exception as e:
                # One broken evaluator must not poison the whole call —
                # surface as a failed pseudo-score so the operator can
                # see the regression in /calls/ rather than 500ing.
                ev_scores = []
                scores_payload.append({
                    "evaluator_id":   ev_cfg.get("id", ev_type),
                    "evaluator_kind": ep_kind,
                    "layer":          int(spec.get("layer", 1)),
                    "value":          0.0,
                    "passed":         False,
                    "raw":            {"error": f"{type(e).__name__}: {e}"},
                })
                all_scores.append(False)
                continue
            for s in ev_scores:
                scores_payload.append({
                    "evaluator_id":   s.evaluator_id,
                    "evaluator_kind": s.evaluator_kind,
                    "layer":          s.layer,
                    "value":          s.value,
                    "passed":         s.passed,
                    "raw":            s.raw,
                })
                all_scores.append(s.passed)
        # All-pass aggregation matches the CLI executor (line 597
        # of local_executor.py).  Empty score set = vacuous pass.
        passed = all(all_scores) if all_scores else True

    # --- Phase 3: persist (short transaction) ------------------------
    # Conn re-acquired here; provider call ran with no pool slot held.
    rec = LiveCallRecord(
        row_id=row_id, raw_input=body.input, raw_expected=body.expected,
        output=output, passed=passed and error is None,
        n_scores=len(scores_payload),
        cost_usd=cost_usd, latency_ms=latency_ms,
        tags=body.tags, scores=scores_payload,
        provider=provider_name, model=model, error=error,
    )
    try:
        with engine.begin() as conn:
            apply_rls_context(
                conn, org_id=principal.org_id, is_admin=principal.is_admin,
            )
            run_id   = ensure_live_run(
                conn, project_id=project_id, project_name=project_name,
            )
            trial_id = ensure_live_trial(
                conn, run_id=run_id, project_id=project_id,
                provider_id=provider_id, provider=provider_name, model=model,
            )
            record_call(
                conn, run_id=run_id, trial_id=trial_id,
                project_id=project_id, rec=rec,
            )
            # PROXY-3.5 — chain-linked audit event for this call.
            # ``emit_event`` handles the prev_event_hash chaining
            # + IntegrityError-retry on concurrent writers; we just
            # provide the actor + PROV subject context.  ``kind`` is
            # ``provider.called`` on success or ``provider.failed``
            # when the upstream call errored (timeout / 429 / 5xx).
            # The row's input + output are passed-through; the
            # audit core's ``redact_secrets`` strips key-shaped
            # fields before hashing.
            ev_kind = "provider.failed" if error else "provider.called"
            emit_event(
                conn,
                kind=ev_kind,
                run_id=run_id,
                project_id=project_id,
                trial_id=trial_id,
                row_id=row_id,
                actor_id=principal.key_id,
                # The proxy actor is always an API key (no human-in-the-
                # loop ingest path here); the existing audit vocabulary
                # in evalguard_evaluators.audit accepts free-form actor
                # types and the CLI uses "cli" / "gha" — "api_key" makes
                # the source unambiguous to anyone tailing the chain.
                actor_type="api_key",
                # Round-5 ultra-review (Correctness K): empty
                # ``actor_meta`` strips useful triage context.  Capture
                # the key's scope set so an auditor reading the chain
                # knows which permissions were in effect — without
                # capturing the secret itself (only ``key_id`` lands).
                actor_meta={"scopes": list(principal.scopes)},
                subject_id=f"{provider_name}:{model}",
                inputs=body.input,
                outputs=output if error is None else None,
                payload={
                    "provider":     provider_name,
                    "model":        model,
                    "error":        error,
                    "is_rate_limited": is_rate_limited,
                    "is_cost_capped":  is_cost_capped,
                    "passed":       passed and error is None,
                    "n_scores":     len(scores_payload),
                },
                cost_usd=cost_usd,
                duration_ms=latency_ms,
            )
            # Post-write cost-cap detection (round-4 ultra-review,
            # Agent-1 B / Agent-3 C).  The Phase-1 cap check is
            # advisory: two concurrent calls can both pass it when
            # the accumulated cost is just under the cap, then both
            # write and overshoot.  Re-read the new accumulated cost
            # WITHIN the same transaction (so we see our own write
            # and any concurrent committed write); if it overshot,
            # log a WARN with the overshoot amount.  We don't refund
            # — the provider was already charged — but the operator
            # gets a structured audit trail and the row still lands
            # in /calls/ so the call is visible.
            if cost_cap_usd > 0 and cost_usd > 0:
                new_total = conn.execute(
                    text("SELECT cost_usd FROM runs WHERE run_id = :rid"),
                    {"rid": run_id},
                ).scalar()
                if new_total is not None and new_total > cost_cap_usd:
                    overshoot = float(new_total) - cost_cap_usd
                    logger.warning(
                        '{"evt":"cost_cap_overshot","project_id":%r,'
                        '"run_id":%r,"row_id":%r,"cap_usd":%.4f,'
                        '"new_total_usd":%.4f,"overshoot_usd":%.4f}',
                        project_id, run_id, row_id,
                        cost_cap_usd, float(new_total), overshoot,
                    )
    except Exception:
        # Phase 3 failed AFTER the provider call (Phase 2).  The
        # provider was charged, but the row isn't recorded — the
        # cost is silently lost from EvalGuard's perspective.
        # Log CRITICAL so operators can reconcile via the provider's
        # own billing dashboard (round-4 review-pass A from Agent-1).
        #
        # Round-5 ultra-review (Security K + Correctness E): the
        # earlier ``error is None and cost_usd > 0`` gate suppressed
        # the log on partial-failure paths — e.g. chain-retry
        # exhaustion AFTER a successful provider call that ALSO
        # carried an upstream-error indicator (rare but possible
        # for providers that return an error AND charge anyway).
        # Drop the ``error is None`` half: cost_usd > 0 means a
        # charge happened; the operator needs the reconciliation
        # breadcrumb regardless of whether the response was a
        # success or a soft-failure.
        if cost_usd > 0:
            logger.critical(
                '{"evt":"phase3_failed_after_provider_charge","project_id":%r,'
                '"row_id":%r,"provider":%r,"model":%r,"cost_usd_lost":%.6f,'
                '"latency_ms":%d}',
                project_id, row_id, provider_name, model,
                cost_usd, latency_ms,
            )
        raise

    # Provider-failed calls surface as the most useful status for
    # the caller's retry logic.  Rate-limit errors get 429 + a
    # conservative Retry-After so a polite client backs off cleanly
    # (and a misbehaving one is at least labeled correctly in
    # ``/calls/?tab=failures``).  Everything else stays 502.
    if error is not None:
        if is_cost_capped:
            # 402 Payment Required is the semantically correct status —
            # the caller's request is well-formed but the project's
            # budget is exhausted.  No Retry-After (the cap resets at
            # UTC midnight when the next day's live run lazy-creates).
            response.status_code = status.HTTP_402_PAYMENT_REQUIRED
        elif is_rate_limited:
            response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
            response.headers["Retry-After"] = "60"
        else:
            response.status_code = status.HTTP_502_BAD_GATEWAY

    return InvokeResponse(
        output=output, passed=passed and error is None,
        cost_usd=cost_usd, latency_ms=latency_ms,
        run_id=run_id, trial_id=trial_id, row_id=row_id,
        scores=scores_payload, error=error,
    )


def _to_prompt(value: Any) -> str:
    """Render a non-string ``input`` deterministically.  ``json.dumps``
    over ``str()`` so the prompt is stable across Python versions and
    matches what the operator would have written by hand."""
    import json
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Round-4 review-pass: upstream-rate-limit detection.


# Provider SDKs raise rate-limit errors with varying class names
# (``openai.RateLimitError``, ``anthropic.RateLimitError``,
# ``httpx.HTTPStatusError``).  Detecting by exception class would
# require an import-per-provider hard-link the proxy doesn't want
# to carry.  Detecting by message substring is portable across
# providers and resilient to SDK version bumps.  The patterns are
# conservative — false negatives (treating a true 429 as 5xx) are
# acceptable; false positives (treating a 5xx as 429) would mislead
# the caller's back-off and are explicitly avoided.
_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit",          # OpenAI: "Rate limit reached"
    "rate_limit",          # JSON-encoded error.code
    "ratelimit",           # Anthropic: "RateLimitError"
    "429",                 # raw status code from httpx / requests
    "too many requests",   # HTTP 429 reason phrase
    "quota",               # Google AI Studio: "Quota exceeded"
    "throttl",             # AWS Bedrock: "ThrottlingException"
)


def _looks_like_rate_limit(exc: Exception) -> bool:
    """Heuristic rate-limit detection from a provider exception.

    Falsely-positive on a benign 5xx whose message happens to
    contain "429" would be worse than falsely-negative on an
    obscure rate-limit format, so the patterns are kept narrow.
    """
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(p in msg for p in _RATE_LIMIT_PATTERNS)
