"""``POST /v1/projects/{slug}/invoke`` — Phase PROXY-2 end-to-end.

These tests exercise the full path: push a config, hit invoke, and
verify (a) the response shape, (b) the day's live run + trial got
lazy-created, (c) the row landed in ``run_rows`` with
``source='live'`` on the parent, (d) aggregates incremented, and (e)
the per-call detail surfaces in ``run_rows.detail_json``.

The mock provider (``providers.mock_provider:MockProvider``) gives
deterministic outputs without an outbound HTTP call, so the suite
stays air-gap-clean.
"""

from __future__ import annotations

import pytest


def _config_yaml(
    *,
    project: str = "default",
    mode: str = "echo",
    with_heuristic: bool = True,
) -> str:
    """Tiny evalguard.yaml that drives the mock provider.

    A LengthHeuristic with a generous max gives us a real scoring
    pass without needing a judge / network call.
    """
    heur = (
        "heuristics:\n"
        "  - id: len-ok\n"
        "    type: length\n"
        "    max: 10000\n"
        "    layer: 1\n"
    ) if with_heuristic else ""
    return (
        "version: 1\n"
        f"project: {project}\n"
        "providers:\n"
        f"  - id: 'mock:m'\n"
        f"    config: {{ mode: {mode}, latency_ms: 0 }}\n"
        + heur
    )


def _push_config(client, headers, slug: str = "default", **kw) -> None:
    r = client.post(
        f"/v1/projects/{slug}/config",
        json={"content": _config_yaml(project=slug, **kw)},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# Happy path


def test_invoke_returns_output_and_records_row(client, auth_headers):
    _push_config(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "hello world", "tags": ["smoke"]},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Echo mode → output is the input.
    assert body["output"] == "hello world"
    assert body["passed"] is True
    assert body["run_id"].startswith("run_live")
    assert body["trial_id"].startswith("trial_live")
    assert body["row_id"].startswith("r-")
    assert body["error"] is None
    # At least one score from the length heuristic.
    assert len(body["scores"]) >= 1
    assert all(s["passed"] for s in body["scores"])


def test_invoke_lazy_creates_then_reuses_live_run(client, auth_headers):
    """Two calls land under the SAME run_id (same UTC day) and the
    SAME trial_id (same provider+model).  Aggregates accumulate."""
    _push_config(client, auth_headers)
    r1 = client.post("/v1/projects/default/invoke",
                     json={"input": "a"}, headers=auth_headers)
    r2 = client.post("/v1/projects/default/invoke",
                     json={"input": "b"}, headers=auth_headers)
    assert r1.json()["run_id"]   == r2.json()["run_id"]
    assert r1.json()["trial_id"] == r2.json()["trial_id"]
    # Distinct row ids.
    assert r1.json()["row_id"]   != r2.json()["row_id"]

    # Aggregate verification — the runs.* counter columns are what
    # PROXY-2.5's timeline UI will read; the row from /v1/runs/{id}
    # only exposes the (header-only) payload so we go to the table.
    from sqlalchemy import text as _text
    run_id = r1.json()["run_id"]
    engine = client.app.state.engine
    with engine.connect() as conn:
        row = conn.execute(
            _text("SELECT source, row_count, row_pass_count, row_fail_count, cost_usd "
                  "FROM runs WHERE run_id = :rid"),
            {"rid": run_id},
        ).mappings().fetchone()
    assert row["source"]          == "live"
    assert row["row_count"]       == 2
    assert row["row_pass_count"]  == 2
    assert row["row_fail_count"]  == 0


def test_invoke_appears_in_calls_stream(client, auth_headers):
    _push_config(client, auth_headers)
    inv = client.post("/v1/projects/default/invoke",
                      json={"input": "stream-me"}, headers=auth_headers)
    assert inv.status_code == 200

    r = client.get(
        "/v1/projects/default/calls?tab=recent&limit=10",
        headers=auth_headers,
    )
    assert r.status_code == 200
    calls = r.json()["calls"]
    # The proxied call should be the newest entry.
    assert any(c["row_id"] == inv.json()["row_id"] for c in calls)


def test_invoke_caller_supplied_row_id_round_trips(client, auth_headers):
    _push_config(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x", "row_id": "user-turn-42"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["row_id"] == "user-turn-42"


# ---------------------------------------------------------------------------
# Per-call detail surfacing


def test_invoke_writes_detail_json_for_drill_down(client, auth_headers):
    """The per-row ``detail_json`` blob carries input/output/scores
    so the OBS-2 detail endpoint can drill down on live calls
    without parsing an empty live-run payload_json."""
    import json as _json
    from sqlalchemy import text as _text
    _push_config(client, auth_headers)
    inv = client.post("/v1/projects/default/invoke",
                      json={"input": "drill", "expected": "drill"},
                      headers=auth_headers)
    row_id = inv.json()["row_id"]

    engine = client.app.state.engine
    with engine.connect() as conn:
        rec = conn.execute(
            _text("SELECT detail_json FROM run_rows WHERE row_id = :rid"),
            {"rid": row_id},
        ).mappings().fetchone()
    assert rec is not None
    detail = _json.loads(rec["detail_json"])
    assert detail["input"]    == "drill"
    assert detail["expected"] == "drill"
    assert detail["output"]   == "drill"
    assert detail["provider"] == "mock"
    assert isinstance(detail["scores"], list)


# ---------------------------------------------------------------------------
# Failure modes


def test_invoke_without_config_returns_422(client, auth_headers):
    """A project that's never had a config pushed has nothing to
    invoke against."""
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "push-config" in r.json()["detail"].lower()


def test_push_config_with_no_providers_rejected_at_push(client, auth_headers):
    """Round-4 review-pass: empty ``providers:`` is now caught at
    PUSH time (not at first invoke).  Operators get the validation
    error immediately instead of after their first proxied call.

    The invoke endpoint still has its own ``providers`` check as
    defence-in-depth in case a historical config slipped past
    push validation, but the happy-path failure is here at push."""
    bad = (
        "version: 1\n"
        "project: default\n"
        "providers: []\n"
    )
    push = client.post(
        "/v1/projects/default/config",
        json={"content": bad},
        headers=auth_headers,
    )
    assert push.status_code == 422, push.text
    assert "providers" in push.text.lower()

    # Sanity: with no config pushed, invoke also 422s — exercises
    # the "config absent" branch in invoke._load_latest_config.
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert "push-config" in r.json()["detail"].lower()


def test_invoke_provider_failure_returns_502_and_records_row(client, auth_headers):
    """Provider raises → 502 to the caller, row persisted with
    ``passed=false`` and ``error`` populated so the operator sees
    the failure in /calls/."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0, fail_with: 'boom' }\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=auth_headers,
    )
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 502
    body = r.json()
    assert body["passed"] is False
    assert "boom" in (body["error"] or "")

    # Row landed on the failures tab.
    failures = client.get(
        "/v1/projects/default/calls?tab=failures&limit=5",
        headers=auth_headers,
    )
    assert failures.status_code == 200
    assert any(c["row_id"] == body["row_id"] for c in failures.json()["calls"])


# ---------------------------------------------------------------------------
# Cross-tenant isolation


def test_invoke_cross_org_returns_404(
    client, auth_headers, make_org, make_member_token,
):
    """A member of org-A cannot invoke against org-B's project — same
    404 anti-enumeration shape as everywhere else."""
    make_org("acme")
    member_acme    = make_member_token("org_acme",    name="a")
    member_default = make_member_token("org_default", name="d")

    # ACME provisions a project + pushes a config.
    client.post(
        "/v1/projects",
        json={"slug": "secret", "name": "Secret"},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    client.post(
        "/v1/projects/secret/config",
        json={"content": _config_yaml(project="secret")},
        headers={"Authorization": f"Bearer {member_acme}"},
    )

    # Default-org member can't invoke.
    r = client.post(
        "/v1/projects/secret/invoke",
        json={"input": "leak attempt"},
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404


def test_invoke_unauthenticated_returns_401(client):
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Review-pass: post-PROXY-2.5 hardening


def test_invoke_rejects_oversized_input(client, auth_headers):
    """A multi-MB ``input`` blob would otherwise bloat ``detail_json``
    indefinitely and DoS the row table.  The ``_MAX_INPUT_BYTES``
    cap (256 KB) rejects with a 422 before any provider call."""
    _push_default_config_simple(client, auth_headers)
    huge = "a" * (300 * 1024)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": huge},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert "too large" in r.text.lower()


def test_invoke_rejects_oversized_expected(client, auth_headers):
    """Round-3 review-pass P: validator sums input + expected + extra;
    a giant ``expected`` alone must also reject."""
    _push_default_config_simple(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "ok", "expected": "x" * (300 * 1024)},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert "too large" in r.text.lower()


def test_invoke_rejects_oversized_extra(client, auth_headers):
    """Round-3 review-pass P: a giant ``extra`` blob alone rejects."""
    _push_default_config_simple(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "ok", "extra": {"blob": "y" * (300 * 1024)}},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text


def test_invoke_rejects_oversized_combined_three_fields(client, auth_headers):
    """Round-3 review-pass P: the validator sums ALL three fields.
    A refactor that accidentally checked each field independently
    would let three ~100KB fields slip through.  Each individually
    is under 256KB; combined is over.  Must reject."""
    _push_default_config_simple(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={
            "input":    "a" * (100 * 1024),
            "expected": "b" * (100 * 1024),
            "extra":    {"data": "c" * (100 * 1024)},
        },
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text


def test_invoke_accepts_combined_three_fields_just_under_cap(client, auth_headers):
    """Round-3 review-pass P: positive case for the combined sum —
    three modest fields summing to <256KB pass."""
    _push_default_config_simple(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={
            "input":    "a" * (60 * 1024),
            "expected": "b" * (60 * 1024),
            "extra":    {"data": "c" * (60 * 1024)},
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text


def test_invoke_provider_429_returns_429_with_retry_after(client, auth_headers):
    """Round-4 review-pass: upstream rate-limit (e.g. OpenAI's
    ``RateLimitError: 429``) must surface as a real 429 + Retry-After
    so the caller's back-off fires correctly.  A generic 502 would
    leave the caller hammering the proxy."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0, fail_with: '429 Rate limit reached' }\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=auth_headers,
    )
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After") == "60"
    body = r.json()
    assert body["passed"] is False
    assert "429" in (body["error"] or "")


def test_invoke_provider_500_still_returns_502(client, auth_headers):
    """Non-rate-limit upstream errors continue to surface as 502.
    Verifies the detector doesn't over-trigger and mark every
    failure as rate-limited."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0, fail_with: 'InternalServerError 500' }\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=auth_headers,
    )
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 502
    assert "Retry-After" not in r.headers


def test_invoke_rate_limit_returns_429_with_retry_after(client, auth_headers):
    """Round-4 #7: per-key rate limit caps requests/minute.  Below
    the cap → 200; above → 429 + Retry-After.  Override the
    config's cap to a tiny value so the test is fast."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "rate_limit_per_minute: 2\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=auth_headers,
    )
    # Two requests under the cap.
    for _ in range(2):
        r = client.post(
            "/v1/projects/default/invoke",
            json={"input": "x"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
    # Third trips the limit.
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 429
    # Round-5 ultra-review (general H / test-quality F10): Retry-After
    # is now DYNAMIC — computed from the oldest in-window timestamp.
    # On a fast machine the math can land on 60 or 61 (rounding +
    # microseconds elapsed); the parallel ``retry_after_reflects_window``
    # test uses the same bounded-range assertion.  Exact ``== "60"``
    # was flaky and only passed because SQLite overhead added 20–50ms
    # of slack between calls.
    retry_after = int(r.headers["Retry-After"])
    assert 1 <= retry_after <= 61, retry_after
    body = r.json()
    assert "rate limit exceeded" in body["detail"].lower()


def test_invoke_rate_limit_is_per_key(
    client, auth_headers, make_org, make_member_token,
):
    """Two distinct keys hitting the same project have INDEPENDENT
    counters — one keyfilling the bucket doesn't throttle the other."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "rate_limit_per_minute: 1\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=auth_headers,
    )
    member_a = make_member_token("org_default", name="alice")
    member_b = make_member_token("org_default", name="bob")
    # Alice fills her bucket.
    r1 = client.post("/v1/projects/default/invoke",
                     json={"input": "x"},
                     headers={"Authorization": f"Bearer {member_a}"})
    assert r1.status_code == 200
    r2 = client.post("/v1/projects/default/invoke",
                     json={"input": "x"},
                     headers={"Authorization": f"Bearer {member_a}"})
    assert r2.status_code == 429
    # Bob is unaffected.
    r3 = client.post("/v1/projects/default/invoke",
                     json={"input": "x"},
                     headers={"Authorization": f"Bearer {member_b}"})
    assert r3.status_code == 200, r3.text


def test_invoke_cost_cap_returns_402_and_records_row(client, auth_headers):
    """Round-4 #7: per-project daily cost cap.  After the cap is
    reached the proxy refuses further calls with 402 Payment
    Required AND records the rejection in /calls/ for operator
    visibility (cost-cap events are rare and important)."""
    import json as _json
    from sqlalchemy import text as _text

    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0, cost_per_call: 0.50 } }]\n"
        "cost_cap_usd_daily: 1.00\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=auth_headers,
    )
    # Two paid calls bring cumulative cost to $1.00 == cap.
    for _ in range(2):
        r = client.post(
            "/v1/projects/default/invoke",
            json={"input": "x"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
    # Third trips the cap.  402 Payment Required.
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["passed"] is False
    assert "cost_cap_exceeded" in body["error"]
    # The rejected row IS persisted with the cap reason — operator
    # sees "cost_cap_exceeded" in /calls/?tab=failures.
    failures = client.get(
        "/v1/projects/default/calls?tab=failures&limit=5",
        headers=auth_headers,
    )
    assert any(c["row_id"] == body["row_id"] for c in failures.json()["calls"])


def test_invoke_cost_cap_disabled_when_unset(client, auth_headers):
    """No ``cost_cap_usd_daily`` in config → unlimited spending
    (back to the pre-#7 behaviour).  Required for backward compat:
    existing configs that don't know about the field continue to
    work without surprise refusals."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0, cost_per_call: 99.0 } }]\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=auth_headers,
    )
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text


def test_invoke_rate_limit_retry_after_reflects_window(client, auth_headers):
    """Round-4 ultra-review (Agent-1 J): ``Retry-After`` was a
    hardcoded ``60`` regardless of the configured cap.  With a tight
    cap (2/min) the client could legitimately retry well before 60s.
    The dynamic value should be ≤ 61 and ≥ 1 (the time until the
    oldest in-window timestamp ages out)."""
    cfg = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "rate_limit_per_minute: 2\n"
    )
    client.post("/v1/projects/default/config",
                json={"content": cfg}, headers=auth_headers)
    for _ in range(2):
        client.post("/v1/projects/default/invoke",
                    json={"input": "x"}, headers=auth_headers)
    r = client.post("/v1/projects/default/invoke",
                    json={"input": "x"}, headers=auth_headers)
    assert r.status_code == 429
    retry_after = int(r.headers["Retry-After"])
    # Just-after-burst: oldest timestamp ~ a few ms old, so
    # retry-after ≈ 60s.  Bounds keep the assertion robust against
    # CI clock jitter.
    assert 1 <= retry_after <= 61, retry_after


def test_invoke_cost_cap_concurrent_overshoot_logs_warn(
    client, auth_headers, monkeypatch, caplog,
):
    """Round-4 ultra-review (Agent-1 B / Agent-3 C): two concurrent
    requests can both pass the Phase-1 advisory check, then both
    write — overshooting the cap.  The post-write detection in
    Phase 3 must log a structured ``cost_cap_overshot`` event so
    operators can reconcile.

    Simulation: pre-populate today's live run with cost = $0.99,
    cap = $1.00.  One $0.50 call passes Phase 1 (within cap) and
    overshoots in Phase 3 to $1.49.  Assert WARN log fires."""
    import logging
    from sqlalchemy import text as _text

    cfg = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0, cost_per_call: 0.50 } }]\n"
        "cost_cap_usd_daily: 1.00\n"
    )
    client.post("/v1/projects/default/config",
                json={"content": cfg}, headers=auth_headers)

    # One call to lazy-create today's run + accumulate $0.50.
    r1 = client.post("/v1/projects/default/invoke",
                     json={"input": "x"}, headers=auth_headers)
    assert r1.status_code == 200

    # Backdoor: bump the run's cost to $0.99 so the NEXT call's
    # Phase-1 check passes ($0.99 < $1.00) but Phase 3 overshoots
    # to $1.49.  Simulates the lost-update race without needing
    # actual concurrency in the sync TestClient.
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            _text("UPDATE runs SET cost_usd = 0.99 "
                  "WHERE project_id = (SELECT project_id FROM projects WHERE slug='default') "
                  "AND source = 'live'"),
        )

    caplog.set_level(logging.WARNING, logger="evalguard.api.invoke")
    r2 = client.post("/v1/projects/default/invoke",
                     json={"input": "y"}, headers=auth_headers)
    assert r2.status_code == 200, r2.text   # call succeeded; provider was charged

    # Test-quality round (Finding 2): the log assertion alone could
    # pass even if the cost-cap branch were a typo'd no-op (no row
    # written, but the log still fires).  Anchor against the
    # OBSERVABLE BEHAVIOUR the warning is supposed to describe.
    warn_lines = [rec.getMessage() for rec in caplog.records
                  if rec.levelname == "WARNING"]
    assert any("cost_cap_overshot" in line for line in warn_lines), (
        f"expected cost_cap_overshot WARN log, got: {warn_lines}"
    )

    # 1. The DB cost MUST have advanced past the cap — the whole point
    #    of the WARN is to call out the overshoot the proxy already
    #    persisted.
    with engine.connect() as conn:
        final_cost = conn.execute(
            _text("SELECT cost_usd FROM runs "
                  "WHERE source = 'live' "
                  "AND project_id = (SELECT project_id FROM projects WHERE slug='default')"),
        ).scalar_one()
    assert final_cost > 1.00, (
        f"cost_usd={final_cost} — overshoot did not actually persist; "
        "log fired but row state is wrong"
    )
    assert final_cost == pytest.approx(1.49, abs=1e-9), (
        f"expected $0.99 (backdoor) + $0.50 (provider) = $1.49, got {final_cost}"
    )

    # 2. The row MUST appear in /calls/ so the operator can drill in.
    #    "Logged but not visible" would be the worst failure mode.
    calls = client.get(
        "/v1/projects/default/calls?tab=recent&limit=10",
        headers=auth_headers,
    ).json()["calls"]
    assert any(c["row_id"] == r2.json()["row_id"] for c in calls), (
        "overshoot row didn't land in /calls/ — operator can't see it"
    )


def test_invoke_phase3_failure_after_provider_logs_critical(
    client, auth_headers, monkeypatch, caplog,
):
    """Round-5 ultra-review (general I + test-quality coverage): the
    CRITICAL log path fires when the provider call succeeded (cost
    was charged) but Phase 3 couldn't record the row.  Without
    this log line the operator has no reconciliation breadcrumb.

    Reproduces: monkeypatch ``ensure_live_run`` to raise after the
    provider call has returned, then assert the CRITICAL structured
    log fires AND the request surfaces as a 500 (the row isn't
    written, so the caller sees the error rather than a silent
    success)."""
    import logging
    import evalguard_api.routes.invoke as invoke_module

    def _phase3_explodes(*args, **kwargs):
        raise RuntimeError("simulated phase-3 DB unreachable")

    # The function is imported by name into the module; patching the
    # module-level binding catches the route's reference.
    monkeypatch.setattr(invoke_module, "ensure_live_run", _phase3_explodes)

    # The CRITICAL log gates on ``cost_usd > 0`` (no charge → nothing
    # to reconcile).  Push a paid config so the provider call
    # returns a non-zero cost and the gate falls through.
    paid_cfg = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0, cost_per_call: 0.42 } }]\n"
    )
    client.post(
        "/v1/projects/default/config",
        json={"content": paid_cfg},
        headers=auth_headers,
    )
    caplog.set_level(logging.CRITICAL, logger="evalguard.api.invoke")

    # Starlette's TestClient defaults to raising server-side
    # exceptions back through the test (``raise_server_exceptions=True``).
    # That's the right behaviour to verify here — the route handler
    # explicitly re-raises after logging, so the exception leaving
    # the handler IS the contract.  A future change that silently
    # swallowed the exception would not raise here, and the
    # CRITICAL log assertion would still fire — but the bug would
    # be visible because the test would no longer raise.
    with pytest.raises(RuntimeError, match="simulated phase-3 DB unreachable"):
        client.post(
            "/v1/projects/default/invoke",
            json={"input": "x"},
            headers=auth_headers,
        )

    critical_lines = [rec.getMessage() for rec in caplog.records
                      if rec.levelname == "CRITICAL"]
    assert any("phase3_failed_after_provider_charge" in line
               for line in critical_lines), (
        f"expected phase3 CRITICAL log naming the lost cost, got: {critical_lines}"
    )
    # Anchor: the log entry must contain the cost_usd_lost field
    # (the whole point of the breadcrumb is reconciliation against
    # the provider billing dashboard).  Without this check, a log
    # like ``phase3_failed_after_provider_charge: ok`` would pass.
    assert any("cost_usd_lost" in line for line in critical_lines), (
        f"CRITICAL log must include cost_usd_lost field, got: {critical_lines}"
    )


def test_invoke_releases_db_conn_during_provider_call(
    client, auth_headers, monkeypatch,
):
    """Round-4 review-pass #6: the DB connection MUST be released
    back to the pool before the provider call runs, otherwise a
    pool of size 10 + 10 slow providers = pool exhaustion + every
    other endpoint blocks.

    The test stalls the provider for 100ms and asserts that during
    the stall the pool's checked-out count is 0 (or at most reflects
    other in-flight requests).  Without the refactor this would
    show ``checked_out == 1`` for the duration of the provider call.
    """
    import asyncio
    from evalguard_evaluators.providers.mock_provider import MockProvider

    engine = client.app.state.engine
    checked_out_during_provider: list[int] = []

    async def _slow_complete(self, prompt, *, model, params=None):
        # Capture the pool state mid-call.  Sleep is short enough
        # not to slow the suite, long enough that the sample is
        # taken after Phase-1 commits and before Phase-3 starts.
        await asyncio.sleep(0.05)
        checked_out_during_provider.append(engine.pool.checkedout())
        from evalguard_evaluators.base import ProviderResult
        return ProviderResult(
            output=prompt, cost_usd=0.0, latency_ms=50, raw={},
        )

    monkeypatch.setattr(MockProvider, "complete", _slow_complete)

    _push_default_config_simple(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert len(checked_out_during_provider) == 1
    # The invoke route's own conn must not be holding a slot.  Other
    # requests in flight from the same TestClient are theoretically
    # possible but the sync TestClient is single-threaded per call,
    # so the only conn that could be checked out is the invoke
    # request's — which the refactor releases before the provider
    # call.  Strict equality is the right assertion.
    assert checked_out_during_provider[0] == 0, (
        f"expected pool slot released during provider call, "
        f"got checkedout={checked_out_during_provider[0]}"
    )


def test_invoke_provider_timeout_returns_502_and_records_row(
    client, auth_headers, monkeypatch,
):
    """Round-3 review-pass O: a hung upstream provider must trip
    ``asyncio.wait_for`` and surface as 502, with the row persisted
    under the failures tab and the error message naming the timeout.

    Setup: monkeypatch ``_PROVIDER_CALL_TIMEOUT_S`` to 0.05s and
    push a config that uses the mock provider's ``never_resolve``
    behaviour via ``asyncio.sleep`` inside a custom complete().
    """
    import asyncio
    import evalguard_api.routes.invoke as invoke_module

    # Shrink the timeout so the test doesn't burn 60s.
    monkeypatch.setattr(invoke_module, "_PROVIDER_CALL_TIMEOUT_S", 0.05)

    # Patch the mock provider's ``complete`` so it hangs forever
    # (until asyncio cancels it).  Direct attribute swap is cheaper
    # than maintaining a "hang" mode in MockProvider itself.
    from evalguard_evaluators.providers.mock_provider import MockProvider

    async def _hang(self, prompt, *, model, params=None):
        await asyncio.sleep(10.0)  # well past the 0.05s cap
        raise AssertionError("provider should have been cancelled")

    monkeypatch.setattr(MockProvider, "complete", _hang)

    _push_default_config_simple(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["passed"] is False
    assert "timeout" in (body["error"] or "").lower()

    # Row landed under failures tab.
    failures = client.get(
        "/v1/projects/default/calls?tab=failures&limit=5",
        headers=auth_headers,
    )
    assert any(c["row_id"] == body["row_id"] for c in failures.json()["calls"])


def test_invoke_redacts_secrets_in_detail_json(client, auth_headers):
    """Defence-in-depth: an ``input`` carrying an accidental
    ``api_key`` field shouldn't surface verbatim in detail_json.
    Operators inspecting /calls/ shouldn't see leaked secrets."""
    import json as _json
    from sqlalchemy import text as _text
    _push_default_config_simple(client, auth_headers)
    inv = client.post(
        "/v1/projects/default/invoke",
        json={"input": {"prompt": "ok", "api_key": "evk_should_not_leak"}},
        headers=auth_headers,
    )
    assert inv.status_code == 200
    row_id = inv.json()["row_id"]

    engine = client.app.state.engine
    with engine.connect() as conn:
        rec = conn.execute(
            _text("SELECT detail_json FROM run_rows WHERE row_id = :rid"),
            {"rid": row_id},
        ).mappings().fetchone()
    detail = _json.loads(rec["detail_json"])
    assert detail["input"]["api_key"] == "***"
    assert detail["input"]["prompt"]  == "ok"


def _push_default_config_simple(client, headers):
    """Echo-mode config without a length heuristic — keeps these
    review-pass tests independent of evaluator details."""
    yaml_blob = (
        "version: 1\n"
        "project: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": yaml_blob},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# live.py id determinism


def test_live_run_id_deterministic_for_same_day():
    """Same project + same UTC day → same run_id, every time.
    Different day → different run_id."""
    from evalguard_api.live import live_run_id
    a = live_run_id("proj_x", "2026-05-29")
    b = live_run_id("proj_x", "2026-05-29")
    c = live_run_id("proj_x", "2026-05-30")
    assert a == b
    assert a != c
    assert a.startswith("run_live")
    # 8 ("run_live") + 16 hex = 24 chars.
    assert len(a) == 24


def test_live_trial_id_deterministic_for_same_provider():
    from evalguard_api.live import live_trial_id
    a = live_trial_id("run_live1234", "openai:gpt-4o-mini", "gpt-4o-mini")
    b = live_trial_id("run_live1234", "openai:gpt-4o-mini", "gpt-4o-mini")
    c = live_trial_id("run_live1234", "openai:gpt-4o",      "gpt-4o")
    assert a == b
    assert a != c
    assert a.startswith("trial_live")
    assert len(a) == 26


def test_live_run_id_does_not_collide_across_projects():
    from evalguard_api.live import live_run_id
    a = live_run_id("proj_a", "2026-05-29")
    b = live_run_id("proj_b", "2026-05-29")
    assert a != b


def test_invoke_actually_uses_live_run_id_for_run_creation(client, auth_headers):
    """Test-quality round (Finding 3): the three pure unit tests above
    are necessary but insufficient — they don't prove the production
    path ACTUALLY routes through ``live_run_id``.  A refactor that
    silently swapped the id derivation for another function would
    leave the unit tests green but break the day's-run-reuse semantic.

    This test bridges the gap: it asks the route layer for the
    run_id and asserts it matches what ``live_run_id`` produces for
    today's project."""
    from evalguard_api.live import live_run_id, utc_date_str
    from sqlalchemy import text as _text

    _push_default_config_simple(client, auth_headers)
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": "x"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    returned_run_id = r.json()["run_id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        project_id = conn.execute(
            _text("SELECT project_id FROM projects WHERE slug = 'default'"),
        ).scalar_one()
    expected = live_run_id(project_id, utc_date_str())
    assert returned_run_id == expected, (
        f"invoke returned {returned_run_id!r}, but live_run_id() "
        f"for (project_id={project_id!r}, today) = {expected!r}. "
        "If these diverge the day's-run-reuse invariant breaks."
    )


# ---------------------------------------------------------------------------
# Source filter exposes 'live'


def test_runs_list_source_live_returns_live_runs(client, auth_headers):
    _push_config(client, auth_headers)
    client.post("/v1/projects/default/invoke",
                json={"input": "x"}, headers=auth_headers)
    r = client.get("/v1/runs?source=live", headers=auth_headers)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["source"] == "live"
    assert runs[0]["run_id"].startswith("run_live")
