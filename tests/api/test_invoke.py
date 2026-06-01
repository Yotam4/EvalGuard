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


def test_live_run_id_deterministic_for_same_day(client, auth_headers):
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


def test_live_trial_id_deterministic_for_same_provider(client, auth_headers):
    from evalguard_api.live import live_trial_id
    a = live_trial_id("run_live1234", "openai:gpt-4o-mini", "gpt-4o-mini")
    b = live_trial_id("run_live1234", "openai:gpt-4o-mini", "gpt-4o-mini")
    c = live_trial_id("run_live1234", "openai:gpt-4o",      "gpt-4o")
    assert a == b
    assert a != c
    assert a.startswith("trial_live")
    assert len(a) == 26


def test_live_run_id_does_not_collide_across_projects(client, auth_headers):
    from evalguard_api.live import live_run_id
    a = live_run_id("proj_a", "2026-05-29")
    b = live_run_id("proj_b", "2026-05-29")
    assert a != b


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
