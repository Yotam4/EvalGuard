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
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.engine import Connection


logger = logging.getLogger("evalguard.api.invoke")

from evalguard_api.auth import Principal, require_principal
from evalguard_api.db import resolve_project_or_404
from evalguard_api.deps import get_conn
from evalguard_api.live import (
    LiveCallRecord, ensure_live_run, ensure_live_trial,
    parse_provider_id, record_call,
)
from evalguard_api.models import InvokeRequest, InvokeResponse


router = APIRouter()


# Hard ceiling on one proxied call's provider latency.  An upstream
# LLM that hangs forever would otherwise pin a worker; 60s is generous
# for a single completion while still catching real outages.  A future
# slice can make this per-project via the stored config.
_PROVIDER_CALL_TIMEOUT_S: float = 60.0


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
    conn: Connection = Depends(get_conn),
    principal: Principal = Depends(require_principal),
) -> InvokeResponse:
    """Forward one call to the project's configured provider, score
    it, record the row under today's live run, and return the LLM
    output synchronously.

    The first provider listed in the stored config drives the call;
    multi-provider routing is a separate slice (the proxy is the
    gateway, not the A/B harness).
    """
    project = _resolve_project(conn, principal, project_slug)
    cfg = _load_latest_config(conn, project["project_id"])

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
    error: str | None = None
    # Round-4 review-pass: differentiate upstream throttling from
    # generic upstream errors so the caller's retry logic does the
    # right thing.  ``True`` here forces a 429 + Retry-After in the
    # response below; otherwise a generic 502 fires.  Detection is
    # by string match against the exception message — providers
    # vary in exception class names (httpx.HTTPStatusError,
    # openai.RateLimitError, anthropic.RateLimitError, …) so a
    # text-based test is the portable choice.
    is_rate_limited = False
    output = ""
    cost_usd = 0.0
    latency_ms = 0
    t0 = time.monotonic()
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
        error = f"{type(e).__name__}: {e}"
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

    # --- persist --------------------------------------------------------------
    run_id   = ensure_live_run(
        conn, project_id=project["project_id"],
        project_name=project["name"],
    )
    trial_id = ensure_live_trial(
        conn, run_id=run_id, project_id=project["project_id"],
        provider_id=provider_id, provider=provider_name, model=model,
    )
    rec = LiveCallRecord(
        row_id=row_id, raw_input=body.input, raw_expected=body.expected,
        output=output, passed=passed and error is None,
        n_scores=len(scores_payload),
        cost_usd=cost_usd, latency_ms=latency_ms,
        tags=body.tags, scores=scores_payload,
        provider=provider_name, model=model, error=error,
    )
    record_call(
        conn, run_id=run_id, trial_id=trial_id,
        project_id=project["project_id"], rec=rec,
    )

    # Provider-failed calls surface as the most useful status for
    # the caller's retry logic.  Rate-limit errors get 429 + a
    # conservative Retry-After so a polite client backs off cleanly
    # (and a misbehaving one is at least labeled correctly in
    # ``/calls/?tab=failures``).  Everything else stays 502.
    if error is not None:
        if is_rate_limited:
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
