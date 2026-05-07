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

import secrets
from datetime import datetime, timezone
from typing import Any


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

    payloads: list[dict] = []
    for rs in rs_list:
        if not isinstance(rs, dict):
            continue
        payload = _resource_spans_to_run(rs, default_project=default_project)
        if payload is not None:
            payloads.append(payload)
    return payloads


def _resource_spans_to_run(
    rs: dict,
    *,
    default_project: str,
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

    for scope in (rs.get("scopeSpans") or []):
        if not isinstance(scope, dict):
            continue
        for span in (scope.get("spans") or []):
            if not isinstance(span, dict):
                continue
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

    if not rows:
        return None

    # Synthesize the run shape. Provider/model live on the *trial*
    # in EvalGuard's schema; we use the most-common (provider, model)
    # across the rows.  If the spans disagree, we still pick one for
    # the trial label and the rows preserve their own provider/model.
    provider, model = _pick_provider_model(rows)
    trial_id = f"trial_otlp_{secrets.token_hex(6)}"

    # Move the per-span ``provider`` / ``model`` strings out of each
    # row (those fields exist on Row but are display-only) — and back
    # them with the synthesized trial.
    for r in rows:
        r["trial_id"] = trial_id

    pass_count = sum(1 for r in rows if r["passed"])
    fail_count = len(rows) - pass_count

    started_at  = _ns_to_iso(earliest_started)
    finished_at = _ns_to_iso(latest_finished)

    run_id = f"run_otlp_{secrets.token_hex(8)}"
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
        "cost_usd":       sum(r.get("cost_usd", 0.0) for r in rows),
        "trials": [
            {
                "trial_id":       trial_id,
                "provider_id":    f"{provider}:{model}" if provider and model else (provider or "otlp"),
                "provider":       provider or "otlp",
                "model":          model or "unknown",
                "row_count":      len(rows),
                "row_pass_count": pass_count,
                "row_fail_count": fail_count,
                "cost_usd":       sum(r.get("cost_usd", 0.0) for r in rows),
                "rows":           rows,
                "status":         "passed" if fail_count == 0 else "failed",
                "config":         {"otlp": True, "service_name": res_attrs.get("service.name")},
            },
        ],
        # No assets yet — OTLP doesn't carry prompt-version hashes.
        "assets":         [],
    }
    return payload


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
        "cost_usd":   0.0,    # cost calc deferred until we wire pricing tables to OTLP
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
