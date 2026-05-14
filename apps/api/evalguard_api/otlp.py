"""OTLP/HTTP JSON → EvalGuard run synthesis.

Phase 3a entrypoint: a user instruments their LLM application with
the OpenTelemetry SDK, sets the GenAI semantic-convention attributes
(``gen_ai.system`` / ``gen_ai.request.model`` / ``gen_ai.usage.*`` /
``gen_ai.prompt.*`` / ``gen_ai.completion.*``), and exports spans to
``POST /v1/otlp/v1/traces``. We translate that into a synthetic Run
that looks indistinguishable from a CLI-pushed run from the API
contract's perspective — so every existing endpoint, the UI, and
the audit log see OTLP-derived data the same way.

This module is deliberately schema-driven: it walks the OTLP/HTTP
JSON shape (``ResourceSpans → ScopeSpans → Span``) and returns a
``run_to_dict``-compatible dict ready for ``_persist_run``. No DB
calls, no FastAPI dependencies — pure data transformation so the
parser is unit-testable in isolation.

Reference for the shape we accept:
- OTLP/HTTP JSON: opentelemetry-proto/opentelemetry/proto/trace/v1/trace.proto
  rendered through the proto-to-JSON mapping rules.
- GenAI semantic conventions:
  https://opentelemetry.io/docs/specs/semconv/gen-ai/
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Cardinality caps
#
# OTLP collectors retry whole batches on 5xx — a hostile / buggy client
# could push a 100 MB body full of millions of nested spans. The main.py
# Content-Length gate caps body size, but a small body with deeply nested
# attribute arrays can still allocate excessive memory once parsed.
# These caps mirror ``RunIngest`` in models.py (50 trials, 50_000 rows
# per trial); we reject before allocating.

_MAX_RESOURCE_SPANS: int = 50         # one Run per ResourceSpans, capped at the trial-count limit
_MAX_SPANS_PER_RESOURCE: int = 50_000  # mirrors Trial.rows max


# ---------------------------------------------------------------------------
# OTel attribute helpers
#
# OTLP encodes attributes as ``[{"key": "...", "value": {"stringValue": "..."}}]``
# tagged unions. The lookup helpers normalize that into a flat dict + reader.


def _attrs_to_dict(attrs: list[dict] | None) -> dict[str, Any]:
    """Flatten OTLP's tagged-union attribute list into a plain dict.

    Each entry is ``{"key": str, "value": {<typeKey>: <value>}}`` with
    one of: ``stringValue``, ``intValue``, ``doubleValue``,
    ``boolValue``, ``arrayValue``, ``kvlistValue``. We unwrap
    arrays/kvlists recursively so the returned dict is JSON-shaped.
    """
    out: dict[str, Any] = {}
    for entry in attrs or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        val = entry.get("value")
        if not isinstance(key, str) or not isinstance(val, dict):
            continue
        out[key] = _unwrap_anyvalue(val)
    return out


def _unwrap_anyvalue(v: dict) -> Any:
    """Pull the actual value out of an OTLP ``AnyValue`` envelope."""
    if "stringValue" in v: return v["stringValue"]
    if "intValue"    in v:
        # OTLP sends int as a JSON string to dodge the ECMAScript 2^53
        # limit; coerce back to int for everything we ship.
        try:    return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v: return float(v["doubleValue"])
    if "boolValue"   in v: return bool(v["boolValue"])
    if "arrayValue" in v:
        items = (v["arrayValue"] or {}).get("values") or []
        return [_unwrap_anyvalue(it) for it in items if isinstance(it, dict)]
    if "kvlistValue" in v:
        return _attrs_to_dict((v["kvlistValue"] or {}).get("values") or [])
    if "bytesValue" in v: return v["bytesValue"]
    return None


# ---------------------------------------------------------------------------
# Time helpers — OTLP uses string nanoseconds; we want ISO


def _ns_to_iso(ns_str: str | int | None) -> str | None:
    """Convert OTLP nanosecond timestamps (sent as strings to fit in
    JSON) to ISO-8601 UTC.  Returns None on missing / unparseable
    inputs — the synthetic run's ``started_at`` is allowed to be null
    in the JSON Schema."""
    if ns_str is None: return None
    try:
        ns = int(ns_str)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Public API


class OtlpParseError(ValueError):
    """Bad OTLP payload shape. The server translates to 400."""


def parse_traces(
    body: dict,
    *,
    default_project: str = "default",
    actor_id: str | None = None,
    actor_type: str | None = None,
) -> list[dict]:
    """Convert one OTLP ``ExportTraceServiceRequest`` JSON body into a
    list of synthetic ``run_to_dict``-compatible payloads — one per
    ``ResourceSpans`` entry.

    Each ResourceSpans block typically corresponds to a single service
    instance (one ``service.name`` resource attribute). We map that 1:1
    to a Run; every span carrying ``gen_ai.*`` attributes inside
    becomes a Row.

    Spans without GenAI attributes are skipped — the contract is
    "GenAI traces become evaluable rows", not "every HTTP span". A
    user emitting non-GenAI spans alongside (e.g., a database span)
    isn't an error; we just ignore them.

    Returns an empty list if the body has no spans we recognize,
    rather than raising — gives the OTLP collector a clean
    ``partialSuccess: {}`` response with zero rejected spans.
    """
    if not isinstance(body, dict):
        raise OtlpParseError("OTLP body must be a JSON object.")

    rs_list = body.get("resourceSpans") or []
    if not isinstance(rs_list, list):
        raise OtlpParseError("``resourceSpans`` must be an array.")
    if len(rs_list) > _MAX_RESOURCE_SPANS:
        raise OtlpParseError(
            f"OTLP body contains {len(rs_list)} resourceSpans entries; the "
            f"limit is {_MAX_RESOURCE_SPANS} (one Run per ResourceSpans). "
            "Split the export into smaller batches."
        )

    payloads: list[dict] = []
    for rs in rs_list:
        if not isinstance(rs, dict):
            continue
        payload = _resource_spans_to_run(
            rs,
            default_project=default_project,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        if payload is not None:
            payloads.append(payload)
    return payloads


def _resource_spans_to_run(
    rs: dict,
    *,
    default_project: str,
    actor_id: str | None = None,
    actor_type: str | None = None,
) -> dict | None:
    """One ``ResourceSpans`` → one Run dict. Returns None if no
    GenAI spans are present (caller skips it)."""
    resource = rs.get("resource") or {}
    res_attrs = _attrs_to_dict(resource.get("attributes"))
    project = (
        res_attrs.get("evalguard.project")
        or res_attrs.get("service.name")
        or default_project
    )

    rows: list[dict] = []
    earliest_started: int | None = None
    latest_finished:  int | None = None
    trace_id_hex: str | None = None  # captured for deterministic run_id

    span_count = 0
    for scope in (rs.get("scopeSpans") or []):
        if not isinstance(scope, dict):
            continue
        for span in (scope.get("spans") or []):
            if not isinstance(span, dict):
                continue
            span_count += 1
            if span_count > _MAX_SPANS_PER_RESOURCE:
                raise OtlpParseError(
                    f"ResourceSpans contains more than "
                    f"{_MAX_SPANS_PER_RESOURCE} spans; split the trace "
                    "or aggregate upstream."
                )
            row = _span_to_row(span)
            if row is None:
                continue
            rows.append(row)
            # Track the earliest start / latest finish so the run's
            # outer timestamps reflect the trace's actual span.
            try:
                start = int(span.get("startTimeUnixNano") or 0)
                end   = int(span.get("endTimeUnixNano")   or 0)
            except (TypeError, ValueError):
                start = end = 0
            if start and (earliest_started is None or start < earliest_started):
                earliest_started = start
            if end and (latest_finished is None or end > latest_finished):
                latest_finished = end
            # Capture the first non-empty traceId we see. All spans in
            # one ResourceSpans typically share a trace; if they don't
            # (multi-trace batch), the first one wins — collectors that
            # retry the same batch hash to the same run_id.
            if trace_id_hex is None:
                t = span.get("traceId")
                if isinstance(t, str) and t.strip():
                    trace_id_hex = t.strip().lower()

    if not rows:
        return None

    # Synthesize the run shape. Provider/model live on the *trial*
    # in EvalGuard's schema; we use the most-common (provider, model)
    # across the rows.  If the spans disagree, we still pick one for
    # the trial label and the rows preserve their own provider/model.
    provider, model = _pick_provider_model(rows)

    # Deterministic ids — a collector that retries the same batch
    # produces the same ``run_id`` so the duplicate-run-id 409 in
    # ``ingest_run`` kicks in instead of double-ingesting. Derives
    # from traceId + project so concurrent traces in the same project
    # never collide, and the same trace re-exported to a different
    # project lands as a different run (rare but valid).
    run_id, trial_id = _deterministic_ids(trace_id_hex, project)

    # Move the per-span ``provider`` / ``model`` strings out of each
    # row (those fields exist on Row but are display-only) — and back
    # them with the synthesized trial. Compute cost from token usage
    # now that we know the trial-level model.
    cost_total = 0.0
    for r in rows:
        r["trial_id"] = trial_id
        r["cost_usd"] = _price_row(
            provider=r.get("provider") or provider,
            model=r.get("model") or model,
            tokens=r.get("otlp_token_usage"),
        )
        cost_total += r["cost_usd"]

    pass_count = sum(1 for r in rows if r["passed"])
    fail_count = len(rows) - pass_count

    started_at  = _ns_to_iso(earliest_started)
    finished_at = _ns_to_iso(latest_finished)

    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "run_id":         run_id,
        "project":        project,
        "status":         "passed" if fail_count == 0 else "failed",
        "row_status":     "passed" if fail_count == 0 else "failed",
        "gate_status":    None,    # OTLP traces don't (yet) drive gates
        "started_at":     started_at,
        "finished_at":    finished_at,
        "row_count":      len(rows),
        "row_pass_count": pass_count,
        "row_fail_count": fail_count,
        "cost_usd":       round(cost_total, 6),
        "trials": [
            {
                "trial_id":       trial_id,
                "provider_id":    f"{provider}:{model}" if provider and model else (provider or "otlp"),
                "provider":       provider or "otlp",
                "model":          model or "unknown",
                "row_count":      len(rows),
                "row_pass_count": pass_count,
                "row_fail_count": fail_count,
                "cost_usd":       round(cost_total, 6),
                "rows":           rows,
                "status":         "passed" if fail_count == 0 else "failed",
                "config":         {"otlp": True, "service_name": res_attrs.get("service.name")},
            },
        ],
        # No assets yet — OTLP doesn't carry prompt-version hashes.
        "assets":         [],
    }
    # Synthesize a minimal hash-chained audit block so OTLP runs
    # appear in the audit log the same way CLI-pushed runs do. The
    # chain has exactly one event of kind ``run.started``; richer
    # per-row events can land in a follow-up that maps span lifecycle
    # to audit kinds. ``event_count`` and ``chain_tip`` follow the
    # same recipe ``_hash_event`` uses in the CLI so the chain
    # verifies under the same logic.
    audit_block = _synthesize_audit_block(
        run_id=run_id,
        trace_id_hex=trace_id_hex,
        # ``actor_type`` must match ``ACTOR_TYPES`` enum
        # (cli|ci|api_key|system). OTLP traffic always arrives with a
        # bearer api_key in the route, so that's the right default
        # when the caller doesn't override.
        actor_id=actor_id or "otlp",
        actor_type=actor_type or "api_key",
        n_rows=len(rows),
        n_trials=1,
    )
    payload["audit"] = audit_block
    return payload


def _synthesize_audit_block(
    *,
    run_id: str,
    trace_id_hex: str | None,
    actor_id: str,
    actor_type: str,
    n_rows: int,
    n_trials: int,
) -> dict[str, Any]:
    """Build a one-event audit block for an OTLP-synthesized run.

    Event id and event_hash are derived deterministically from the
    trace id so a collector retry hashes to the same chain tip —
    consistent with the deterministic run_id.
    """
    event_id = (
        f"ev_otlp_{hashlib.sha256(trace_id_hex.encode()).hexdigest()[:16]}"
        if trace_id_hex
        else f"ev_otlp_{secrets.token_hex(8)}"
    )
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    event: dict[str, Any] = {
        "event_id":        event_id,
        "kind":            "run.started",
        "run_id":          run_id,
        "actor_id":        actor_id,
        "actor_type":      actor_type,
        "started_at":      now,
        "prev_event_hash": None,
        "payload": {
            "source":      "otlp",
            "trace_id":    trace_id_hex,
            "n_rows":      n_rows,
            "n_trials":    n_trials,
        },
    }
    # event_hash = sha256(canonical(event - event_hash)) — same recipe
    # as the CLI's ``_hash_event`` so the existing chain-verify path
    # in audit.py validates these without special-casing.
    import json as _json
    canonical = _json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    event["event_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "actor_id":    actor_id,
        "actor_type":  actor_type,
        "actor_meta":  {"source": "otlp"},
        "event_count": 1,
        "chain_tip":   event["event_hash"],
        "events":      [event],
    }


# ---------------------------------------------------------------------------
# Deterministic id derivation — makes OTLP ingest idempotent under
# collector-retry semantics.


def _deterministic_ids(trace_id_hex: str | None, project: str) -> tuple[str, str]:
    """Return ``(run_id, trial_id)`` derived from the trace id.

    If the trace lacks an id (older SDKs / hand-crafted payloads), we
    fall back to a random id — which costs idempotency but is the
    only safe option. Logged in tests so a regression is loud.
    """
    if trace_id_hex:
        digest = hashlib.sha256(
            f"otlp:{project}:{trace_id_hex}".encode("utf-8")
        ).hexdigest()
        # ``run_<>`` regex requires lowercase alnum + ≥8 chars. The
        # full sha256 hex satisfies it; we keep 16 chars for a stable,
        # human-readable run_id.
        run_id = f"run_otlp{digest[:16]}"
        trial_id = f"trial_otlp{digest[16:28]}"
        return run_id, trial_id
    # Fallback — non-idempotent but at least valid id shape.
    return (
        f"run_otlp_{secrets.token_hex(8)}",
        f"trial_otlp_{secrets.token_hex(6)}",
    )


# ---------------------------------------------------------------------------
# Span → Row


def _span_to_row(span: dict) -> dict | None:
    """One OTLP Span with ``gen_ai.*`` attributes → one Row dict.

    Returns None if the span carries no GenAI attributes (caller
    skips it). Status code on the OTel span flows through to
    ``passed`` so a 5xx provider call lands as a failed row.
    """
    attrs = _attrs_to_dict(span.get("attributes"))
    has_gen_ai = any(k.startswith("gen_ai.") for k in attrs)
    if not has_gen_ai:
        return None

    span_id   = (span.get("spanId") or "").strip() or secrets.token_hex(8)
    row_id    = f"otlp_{span_id[:16]}"

    # ``status.code`` is OTel's: 0=UNSET, 1=OK, 2=ERROR. Anything not
    # ERROR counts as passed. ERROR statuses (provider failures, etc.)
    # mark the row failed so aggregate gates can react.
    status   = (span.get("status") or {}) if isinstance(span.get("status"), dict) else {}
    code = status.get("code")
    # OTLP/HTTP JSON renders enums as either ints or strings; accept both.
    is_error = code in (2, "STATUS_CODE_ERROR", "ERROR")
    passed = not is_error

    provider = attrs.get("gen_ai.system")
    model    = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
    in_tokens  = attrs.get("gen_ai.usage.input_tokens")  or attrs.get("gen_ai.usage.prompt_tokens")
    out_tokens = attrs.get("gen_ai.usage.output_tokens") or attrs.get("gen_ai.usage.completion_tokens")

    # Assemble prompt / completion content if the SDK split it across
    # numbered keys (gen_ai.prompt.0.content, gen_ai.completion.0.content,
    # ...); otherwise pass through whatever single string was set.
    prompt_text     = _collect_chat_content(attrs, "gen_ai.prompt")
    completion_text = _collect_chat_content(attrs, "gen_ai.completion")

    # Latency: end - start, in milliseconds.
    try:
        latency_ms = max(
            0,
            (int(span.get("endTimeUnixNano") or 0)
             - int(span.get("startTimeUnixNano") or 0)) // 1_000_000,
        )
    except (TypeError, ValueError):
        latency_ms = 0

    return {
        "row_id":     row_id,
        "passed":     passed,
        "n_scores":   0,
        "provider":   provider,
        "model":      model,
        # Cost is computed at the trial level in
        # ``_resource_spans_to_run`` so it can use the trial's
        # canonical provider/model when the per-span value is missing.
        "cost_usd":   0.0,
        "latency_ms": latency_ms,
        "cache_hit":  False,
        "tags":       _row_tags(attrs, span),
        "input":      prompt_text,
        "expected":   None,
        "output":     completion_text,
        "scores":     [],
        # Pass through the raw span attributes so the UI run-detail
        # page can show them under ``extra``. ``Row`` is
        # ``extra='allow'`` in models.py.
        "otlp_attributes": attrs,
        "otlp_token_usage": {
            "input_tokens":  in_tokens,
            "output_tokens": out_tokens,
        } if (in_tokens or out_tokens) else None,
    }


def _collect_chat_content(attrs: dict, prefix: str) -> str | None:
    """SDKs may set ``gen_ai.prompt.0.content``, ``.1.content``, …
    Concatenate them in order so the row's ``input`` / ``output``
    are searchable strings. If only the unindexed key is set, return
    that. Returns None when neither shape is present."""
    indexed = sorted(
        (k, v) for k, v in attrs.items()
        if k.startswith(prefix + ".") and k.endswith(".content")
    )
    if indexed:
        return "\n".join(str(v) for _, v in indexed if v is not None)
    plain = attrs.get(prefix)
    return str(plain) if plain is not None else None


def _row_tags(attrs: dict, span: dict) -> list[str]:
    """Tag the row with whatever OTel labels are useful for the UI's
    tag filter — operation name, span kind, request type."""
    tags: list[str] = []
    name = span.get("name")
    if isinstance(name, str) and name:
        tags.append(f"op:{name}")
    op = attrs.get("gen_ai.operation.name")
    if isinstance(op, str):
        tags.append(f"gen_ai_op:{op}")
    return tags


def _price_row(
    *,
    provider: str | None,
    model: str | None,
    tokens: dict | None,
) -> float:
    """Cost of one OTLP-derived row from its token usage, in USD.

    Reuses the OpenAI provider's pricing table when ``provider ==
    'openai'``. Other providers fall through to 0.0 until their
    pricing tables ship — same fail-soft contract as a model the
    table doesn't list (cost is 0, but the row still lands so token
    counts are visible).

    The import is lazy because ``evalguard_evaluators`` is an
    optional dependency of the API (the CLI carries it, the server
    only needs it for OTLP cost). If the package is absent, every
    OTLP row prices to 0 — the current pre-fix behaviour.
    """
    if not provider or not model or not tokens:
        return 0.0
    if str(provider).lower() != "openai":
        return 0.0
    try:
        from evalguard_evaluators.providers.openai_provider import _PRICING
    except Exception:  # noqa: BLE001 — pricing is best-effort
        return 0.0
    in_price, out_price = _PRICING.get(model, (0.0, 0.0))
    if in_price == 0.0 and out_price == 0.0:
        return 0.0
    in_tokens  = _safe_int(tokens.get("input_tokens"))
    out_tokens = _safe_int(tokens.get("output_tokens"))
    return round(
        (in_tokens * in_price + out_tokens * out_price) / 1_000_000,
        6,
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pick_provider_model(rows: list[dict]) -> tuple[str | None, str | None]:
    """Most-common (provider, model) across the row set, or
    (None, None) if every row is missing both. Used only to label the
    synthetic trial — per-row provider/model is preserved on each
    Row."""
    counts: dict[tuple[str | None, str | None], int] = {}
    for r in rows:
        key = (r.get("provider"), r.get("model"))
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None, None
    return max(counts.items(), key=lambda kv: kv[1])[0]
