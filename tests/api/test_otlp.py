"""``POST /v1/otlp/v1/traces`` — OTLP/HTTP JSON ingest of GenAI spans.

Tests use the standard OTLP/HTTP JSON shape that any OTel SDK (or
the OTel collector) would send. The parser is exercised in
isolation; the route is exercised via TestClient against a real
in-memory app + Alembic-migrated DB.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Synthetic OTLP payload helpers — match the OTLP/HTTP JSON shape
# (proto-to-JSON mapping rules: int64 fields are JSON strings;
# attribute values use the AnyValue tagged union).


def _str_attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _int_attr(key: str, value: int) -> dict:
    # OTLP/HTTP JSON renders int64 as a string to dodge ECMAScript's
    # 2^53 limit; the parser must accept that form.
    return {"key": key, "value": {"intValue": str(value)}}


def _gen_ai_span(span_id: str, *, model="gpt-4o-mini", error=False, prompt=None, completion=None) -> dict:
    attrs = [
        _str_attr("gen_ai.system", "openai"),
        _str_attr("gen_ai.request.model", model),
        _str_attr("gen_ai.response.model", model),
        _int_attr("gen_ai.usage.input_tokens", 12),
        _int_attr("gen_ai.usage.output_tokens", 34),
        _str_attr("gen_ai.operation.name", "chat"),
    ]
    if prompt is not None:
        attrs.append(_str_attr("gen_ai.prompt.0.role", "user"))
        attrs.append(_str_attr("gen_ai.prompt.0.content", prompt))
    if completion is not None:
        attrs.append(_str_attr("gen_ai.completion.0.role", "assistant"))
        attrs.append(_str_attr("gen_ai.completion.0.content", completion))
    return {
        "traceId":          "abcdef0123456789abcdef0123456789",
        "spanId":            span_id,
        "name":              "chat openai gpt-4o-mini",
        "kind":              2,            # CLIENT
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano":   "1700000000123000000",  # 123 ms
        "attributes":        attrs,
        "status":            {"code": 2 if error else 1},
    }


def _resource_spans(service_name: str, *spans: dict) -> dict:
    return {
        "resource": {
            "attributes": [_str_attr("service.name", service_name)],
        },
        "scopeSpans": [
            {
                "scope": {"name": "openai-instrumentor", "version": "0.1.0"},
                "spans": list(spans),
            },
        ],
    }


def _otlp_body(*resource_spans: dict) -> dict:
    return {"resourceSpans": list(resource_spans)}


# ---------------------------------------------------------------------------
# Route smoke


def test_otlp_ingest_returns_otlp_shaped_response(client, auth_headers):
    body = _otlp_body(_resource_spans(
        "rag-service",
        _gen_ai_span("span0001", prompt="What is X?", completion="X is ..."),
    ))
    r = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    j = r.json()
    # OTLP/HTTP success spec — empty partialSuccess.
    assert j["partialSuccess"] == {}
    # Our debugging envelope.
    assert j["evalguard"]["accepted_runs"] == 1


def test_otlp_ingest_requires_auth(client):
    body = _otlp_body(_resource_spans("svc", _gen_ai_span("s")))
    r = client.post("/v1/otlp/v1/traces", json=body)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Phase 3c — sampler at the route boundary


def _client_with_sample_rate(tmp_path, rate: float):
    """Spin up an isolated app with ``otlp_sample_rate=rate``. Done
    here (not via a conftest fixture) so the rate is right next to
    the assertion that depends on it — the failure mode otherwise is
    "tests interact" via shared fixtures, which is exactly the kind
    of action-at-a-distance that wastes debug time."""
    from fastapi.testclient import TestClient
    from evalguard_api.config import Settings
    from evalguard_api.main import build_app
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/server.db",
        api_key="test-secret",
        cors_origins=("*",),
        otlp_sample_rate=rate,
    )
    app = build_app(settings=s)
    return TestClient(app)


def test_otlp_sampler_rate_zero_drops_every_span(tmp_path, auth_headers):
    """``EVALGUARD_OTLP_SAMPLE_RATE=0`` is the air-gap safety valve:
    accept the request (200 OK so the OTel collector is happy) but
    persist nothing.  The envelope reports zero accepted runs and
    one dropped span so an operator can see the sampler is on."""
    with _client_with_sample_rate(tmp_path, 0.0) as c:
        body = _otlp_body(_resource_spans(
            "rag", _gen_ai_span("span0001"),
        ))
        r = c.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
        assert r.status_code == 200, r.text
        env = r.json()["evalguard"]
        assert env["accepted_runs"]  == 0
        assert env["kept_spans"]     == 0
        assert env["dropped_spans"]  == 1
        assert env["sample_rate"]    == 0.0
        # And no run materialised in the listing.
        listing = c.get("/v1/runs", headers=auth_headers).json()
        assert all(r["source"] != "otlp" for r in listing["runs"])


def test_otlp_sampler_rate_one_accepts_every_span(tmp_path, auth_headers):
    """The default. Two spans in, one synthetic run out, dropped=0."""
    with _client_with_sample_rate(tmp_path, 1.0) as c:
        body = _otlp_body(_resource_spans(
            "rag",
            _gen_ai_span("span0001"),
            _gen_ai_span("span0002"),
        ))
        r = c.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
        env = r.json()["evalguard"]
        assert env["accepted_runs"] == 1
        assert env["kept_spans"]    == 2
        assert env["dropped_spans"] == 0
        assert env["sample_rate"]   == 1.0


def test_otlp_sampler_rate_zero_still_returns_otlp_shape(tmp_path, auth_headers):
    """The OTel collector treats 2xx + ``partialSuccess: {}`` as
    success.  Even when we drop everything, that contract holds —
    otherwise the collector would back off / retry, which would be
    counterproductive at the load-shedding boundary."""
    with _client_with_sample_rate(tmp_path, 0.0) as c:
        body = _otlp_body(_resource_spans("rag", _gen_ai_span("s1")))
        r = c.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["partialSuccess"] == {}


def test_otlp_sampler_decision_is_deterministic_across_requests(tmp_path, auth_headers):
    """Same traceId, same rate → same verdict on a fresh client.
    Pins the stability guarantee — a collector that retries the same
    trace ID after a transient 5xx must get the same outcome the
    second time, otherwise we'd ingest duplicates."""
    body = _otlp_body(_resource_spans(
        "rag",
        # All three share the default traceId from _gen_ai_span.
        _gen_ai_span("span0001"),
        _gen_ai_span("span0002"),
    ))
    accepted_first = None
    for _ in range(3):
        with _client_with_sample_rate(tmp_path / f"db{_!r}", 0.5) as c:
            r = c.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
            kept = r.json()["evalguard"]["kept_spans"]
            if accepted_first is None:
                accepted_first = kept
            else:
                assert kept == accepted_first


def test_otlp_sampler_does_not_split_a_trace(tmp_path, auth_headers):
    """All spans of one traceId share a verdict — never a half-
    rendered trace."""
    body = _otlp_body(_resource_spans(
        "rag",
        _gen_ai_span("span0001"),
        _gen_ai_span("span0002"),
        _gen_ai_span("span0003"),
    ))
    # All three default to the same traceId, so kept_spans must be
    # 0 OR 3 — never 1 or 2.
    for rate in (0.1, 0.3, 0.7):
        with _client_with_sample_rate(tmp_path / f"db{rate}", rate) as c:
            r = c.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
            kept = r.json()["evalguard"]["kept_spans"]
            assert kept in (0, 3), (rate, kept)


def test_invalid_otlp_sample_rate_refuses_to_boot(tmp_path):
    """``Settings.otlp_sample_rate`` outside [0, 1] is a config bug;
    fail loudly rather than silently clamping."""
    from evalguard_api.config import Settings, StartupRefusal, validate_for_startup
    bad = Settings(
        database_url=f"sqlite:///{tmp_path}/x.db",
        api_key="test-secret",
        cors_origins=("https://x",),
        otlp_sample_rate=2.0,
    )
    with pytest.raises(StartupRefusal, match="OTLP_SAMPLE_RATE"):
        validate_for_startup(bad)


# ---------------------------------------------------------------------------
# End-to-end: ingest then read back via /v1/runs


def test_otlp_run_appears_in_runs_list_with_otlp_source(client, auth_headers):
    body = _otlp_body(_resource_spans(
        "rag-service",
        _gen_ai_span("span0001", prompt="Q1", completion="A1"),
        _gen_ai_span("span0002", prompt="Q2", completion="A2"),
    ))
    client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)

    r = client.get("/v1/runs", headers=auth_headers)
    assert r.status_code == 200
    runs = r.json()["runs"]
    # Filter to OTLP-sourced runs (the conftest may push CLI runs too).
    otlp = [x for x in runs if x["source"] == "otlp"]
    assert len(otlp) == 1
    run = otlp[0]
    # ``run_otlp`` (no trailing underscore) — the id is deterministic
    # from sha256(trace_id) so a collector retry hashes to the same
    # value. Matches the regex in models.py ``RunIngest.run_id``.
    assert run["run_id"].startswith("run_otlp")
    assert run["project"] == "rag-service"
    assert run["row_count"] == 2
    assert run["row_pass_count"] == 2
    assert run["status"] == "passed"


def test_otlp_run_detail_carries_synthesized_trial(client, auth_headers):
    body = _otlp_body(_resource_spans(
        "rag-service",
        _gen_ai_span("span0001", prompt="What is the capital of France?", completion="Paris."),
    ))
    post = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert post.status_code == 200
    # The runs endpoint surfaces ``run_id``s; OTLP-derived ones are
    # named ``run_otlp_*``.
    listing = client.get("/v1/runs", headers=auth_headers).json()
    run_id = next(r["run_id"] for r in listing["runs"] if r["source"] == "otlp")

    detail = client.get(f"/v1/runs/{run_id}", headers=auth_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["project"] == "rag-service"
    assert len(body["trials"]) == 1
    trial = body["trials"][0]
    # Provider/model survive the round-trip via the synthesized trial.
    assert trial["provider"] == "openai"
    assert trial["model"]    == "gpt-4o-mini"
    [row] = trial["rows"]
    assert row["passed"] is True
    assert row["latency_ms"] == 123  # endTimeUnixNano - startTimeUnixNano
    # The actual prompt / completion text rides on input / output.
    assert row["input"]  == "What is the capital of France?"
    assert row["output"] == "Paris."


# ---------------------------------------------------------------------------
# Error handling


def test_otlp_status_error_marks_row_failed(client, auth_headers):
    body = _otlp_body(_resource_spans(
        "rag-service",
        _gen_ai_span("span0001", error=True),
    ))
    client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    runs = client.get("/v1/runs", headers=auth_headers).json()["runs"]
    otlp_run = next(r for r in runs if r["source"] == "otlp")
    assert otlp_run["status"] == "failed"
    assert otlp_run["row_fail_count"] == 1


def test_otlp_skips_non_genai_spans(client, auth_headers):
    """A trace that mixes GenAI spans with database / HTTP spans
    must NOT generate rows for the non-GenAI ones."""
    body = _otlp_body(_resource_spans(
        "rag-service",
        _gen_ai_span("span0001"),
        # A non-GenAI span (e.g., db query) — no gen_ai.* attributes.
        {
            "traceId":           "abcdef0123456789abcdef0123456789",
            "spanId":            "spannogenai00000",
            "name":              "SELECT users",
            "kind":              3,
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano":   "1700000000050000000",
            "attributes":        [_str_attr("db.system", "postgresql")],
            "status":            {"code": 1},
        },
    ))
    client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    runs = client.get("/v1/runs", headers=auth_headers).json()["runs"]
    otlp_run = next(r for r in runs if r["source"] == "otlp")
    assert otlp_run["row_count"] == 1


def test_otlp_no_genai_spans_returns_zero_accepted(client, auth_headers):
    """Posting a trace with no GenAI spans is a no-op — 200, but
    no run is created. The OTel collector treats this as success."""
    body = _otlp_body(_resource_spans(
        "rag-service",
        {
            "traceId":           "abcdef0123456789abcdef0123456789",
            "spanId":            "spannogenai00000",
            "name":              "SELECT users",
            "kind":              3,
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano":   "1700000000050000000",
            "attributes":        [_str_attr("db.system", "postgresql")],
            "status":            {"code": 1},
        },
    ))
    r = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["evalguard"]["accepted_runs"] == 0


def test_otlp_rejects_malformed_json(client, auth_headers):
    r = client.post(
        "/v1/otlp/v1/traces",
        content=b"not json",
        headers={**auth_headers, "content-type": "application/json"},
    )
    assert r.status_code == 400


def test_otlp_rejects_resource_spans_not_an_array(client, auth_headers):
    r = client.post(
        "/v1/otlp/v1/traces",
        json={"resourceSpans": "not an array"},
        headers=auth_headers,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Project routing via ``service.name``


def test_otlp_uses_service_name_as_project(client, auth_headers):
    body = _otlp_body(_resource_spans(
        "checkout-service",
        _gen_ai_span("span0001"),
    ))
    client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    runs = client.get("/v1/runs", headers=auth_headers).json()["runs"]
    assert any(r["project"] == "checkout-service" for r in runs)


def test_otlp_evalguard_project_attr_overrides_service_name(client, auth_headers):
    """``evalguard.project`` resource attribute lets a user route a
    trace to a project name distinct from ``service.name`` — useful
    when one service runs evals for many products."""
    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _str_attr("service.name",       "ml-platform"),
                        _str_attr("evalguard.project", "summarizer-v2"),
                    ],
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "x"},
                        "spans": [_gen_ai_span("span0001")],
                    },
                ],
            },
        ],
    }
    client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    runs = client.get("/v1/runs", headers=auth_headers).json()["runs"]
    projects = {r["project"] for r in runs if r["source"] == "otlp"}
    assert "summarizer-v2" in projects
    assert "ml-platform" not in projects


# ---------------------------------------------------------------------------
# Cross-org isolation — same contract as /v1/runs


def test_otlp_lands_in_callers_org_only(
    client, auth_headers, make_org, make_member_token,
):
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    body = _otlp_body(_resource_spans("rag", _gen_ai_span("span0001")))
    client.post(
        "/v1/otlp/v1/traces", json=body,
        headers={"Authorization": f"Bearer {member_acme}"},
    )

    # Member of org_default must NOT see the OTLP run from org_acme.
    runs = client.get(
        "/v1/runs",
        headers={"Authorization": f"Bearer {member_default}"},
    ).json()["runs"]
    assert all(r["source"] != "otlp" for r in runs)
    # The acme member sees their own run.
    runs2 = client.get(
        "/v1/runs",
        headers={"Authorization": f"Bearer {member_acme}"},
    ).json()["runs"]
    assert any(r["source"] == "otlp" for r in runs2)


# ---------------------------------------------------------------------------
# Round-4 review regressions — idempotency, cost pricing, audit chain,
# schema validation, cardinality. Each test pins one finding from the
# review of commit 8b7ff73 ("Phase 3a: OTLP / gen_ai.* ingest").


def test_otlp_retry_with_same_trace_id_is_idempotent(client, auth_headers):
    """OTel collectors retry whole batches on 5xx. A retry of the
    same trace must NOT double-ingest — the run_id is now derived
    deterministically from sha256(trace_id) so the duplicate check
    at the route ack's 200 with ``duplicate_runs: 1`` and no new
    row lands."""
    body = _otlp_body(_resource_spans(
        "rag",
        _gen_ai_span("span0001", prompt="Q", completion="A"),
    ))
    r1 = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert r1.status_code == 200
    assert r1.json()["evalguard"]["accepted_runs"]  == 1
    assert r1.json()["evalguard"]["duplicate_runs"] == 0

    # Same body, second time — collector retry.
    r2 = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["evalguard"]["accepted_runs"]  == 0
    assert r2.json()["evalguard"]["duplicate_runs"] == 1

    # And the runs list shows exactly one OTLP run, not two.
    runs = client.get("/v1/runs", headers=auth_headers).json()["runs"]
    otlp = [r for r in runs if r["source"] == "otlp"]
    assert len(otlp) == 1


def test_otlp_cost_priced_from_token_usage_for_known_openai_models(
    client, auth_headers,
):
    """When the span carries ``gen_ai.system=openai`` and a model in
    the OpenAI pricing table, ``cost_usd`` is non-zero on the row
    AND on the run aggregate. Before the fix, OTLP rows always
    priced to $0 even with token counts present."""
    body = _otlp_body(_resource_spans(
        "rag",
        # _gen_ai_span defaults to gpt-4o-mini + 12 in / 34 out tokens
        _gen_ai_span("span0001", prompt="Q", completion="A"),
    ))
    r = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert r.status_code == 200

    run_id = next(
        rr["run_id"] for rr in client.get("/v1/runs", headers=auth_headers).json()["runs"]
        if rr["source"] == "otlp"
    )
    body = client.get(f"/v1/runs/{run_id}", headers=auth_headers).json()
    # gpt-4o-mini = ($0.15 in, $0.60 out) per 1M tokens
    # → 12 * 0.15 / 1_000_000 + 34 * 0.60 / 1_000_000 = 2.22e-5
    assert body["cost_usd"] > 0
    assert body["trials"][0]["cost_usd"] > 0
    assert body["trials"][0]["rows"][0]["cost_usd"] > 0


def test_otlp_unknown_provider_prices_to_zero(client, auth_headers):
    """A provider the pricing table doesn't know about (e.g.
    'anthropic' until we ship its table) must still ingest cleanly,
    just with cost_usd=0. No NaN, no exception, no skipped row."""
    body = _otlp_body(_resource_spans(
        "rag",
        {
            "traceId": "fedcba9876543210fedcba9876543210",
            "spanId":  "span0001",
            "name":    "anthropic chat",
            "kind":    2,
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano":   "1700000000123000000",
            "attributes": [
                _str_attr("gen_ai.system", "anthropic"),
                _str_attr("gen_ai.request.model", "claude-3-5-sonnet"),
                _int_attr("gen_ai.usage.input_tokens",  100),
                _int_attr("gen_ai.usage.output_tokens", 200),
            ],
        },
    ))
    r = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    run_id = next(
        rr["run_id"] for rr in client.get("/v1/runs", headers=auth_headers).json()["runs"]
        if rr["source"] == "otlp"
    )
    body = client.get(f"/v1/runs/{run_id}", headers=auth_headers).json()
    assert body["cost_usd"] == 0
    assert body["row_count"] == 1


def test_otlp_run_carries_synthesized_audit_chain(client, auth_headers):
    """Every OTLP run must produce a one-event hash-chained audit
    block so the run appears in the audit log alongside CLI-pushed
    runs. Before the fix, OTLP runs had no ``audit`` key at all."""
    body = _otlp_body(_resource_spans("rag", _gen_ai_span("span0001")))
    client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    run_id = next(
        r["run_id"] for r in client.get("/v1/runs", headers=auth_headers).json()["runs"]
        if r["source"] == "otlp"
    )
    body = client.get(f"/v1/runs/{run_id}", headers=auth_headers).json()
    audit = body.get("audit")
    assert audit is not None
    assert audit["event_count"] == 1
    assert audit["chain_tip"]
    assert len(audit["events"]) == 1
    ev = audit["events"][0]
    assert ev["kind"]       == "run.started"
    assert ev["actor_type"] == "api_key"
    # ``event_hash`` is sha256(canonical(event)); check the chain-tip
    # matches so verify_chain would pass without special-casing.
    assert audit["chain_tip"] == ev["event_hash"]


def test_otlp_rejects_too_many_resource_spans(client, auth_headers):
    """Adversarial / buggy clients pushing more than the
    ResourceSpans cardinality cap (one Run per ResourceSpans, max 50)
    get 400 instead of OOM-ing the worker."""
    body = {
        "resourceSpans": [
            _resource_spans(f"svc-{i}", _gen_ai_span(f"span{i:04x}"))
            for i in range(51)
        ],
    }
    r = client.post("/v1/otlp/v1/traces", json=body, headers=auth_headers)
    assert r.status_code == 400
    assert "resourceSpans" in r.json()["detail"]


def test_otlp_payload_round_trips_RunIngest_schema(client, auth_headers):
    """Sanity: the synthesized payload survives RunIngest validation.
    Catches drift where a future otlp.py change emits fields the
    strict RunIngest model rejects (this would otherwise surface as
    a 400 on ingest, not a unit-test failure)."""
    from evalguard_api.models import RunIngest
    from evalguard_api.otlp import parse_traces

    body = _otlp_body(_resource_spans(
        "rag",
        _gen_ai_span("span0001", prompt="Q", completion="A"),
    ))
    [payload] = parse_traces(body, default_project="default")
    # Must not raise — RunIngest is the contract the route enforces.
    RunIngest.model_validate(payload)
