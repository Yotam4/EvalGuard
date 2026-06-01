"""``/v1/projects/{slug}/live/*`` + extended `/calls/` filters.

Phase PROXY-2.5 — verbose live-run inspection.  Tests cover:

- Calls list ``?from=&to=`` half-open window filter.
- Calls list ``?tab=passed`` returns passed rows only.
- Detail endpoint pulls live rows from ``run_rows.detail_json`` so
  drill-down works on proxied calls.
- ``/live/timeline`` returns daily live runs newest-first.
- ``/live/aggregate?from=&to=`` SUMs across the window.
- Cross-org / unauth get the same 404 / 401 anti-enumeration shape.
"""

from __future__ import annotations


_BASIC_CFG = (
    "version: 1\n"
    "project: default\n"
    "providers:\n"
    "  - id: 'mock:m'\n"
    "    config: { mode: echo, latency_ms: 0 }\n"
    "heuristics:\n"
    "  - id: len-ok\n"
    "    type: length\n"
    "    max: 10000\n"
)


def _push_default_config(client, headers):
    r = client.post(
        "/v1/projects/default/config",
        json={"content": _BASIC_CFG},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text


def _invoke(client, headers, *, input: str, expected: str | None = None):
    r = client.post(
        "/v1/projects/default/invoke",
        json={"input": input, "expected": expected},
        headers=headers,
    )
    assert r.status_code in (200, 502), r.text
    return r


def _set_run_started_at(client, run_id: str, started_at: str) -> None:
    """Pin a live run's ``started_at`` for predictable aggregate / timeline tests.
    The proxy stamps it at lazy-create time as ``<YYYY-MM-DD>T00:00:00+00:00``;
    backdating here lets one test cover multiple "days" without sleeping."""
    from sqlalchemy import text
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET started_at = :s WHERE run_id = :rid"),
            {"s": started_at, "rid": run_id},
        )


def _set_row_ingested_at(client, row_id: str, ingested_at: str) -> None:
    from sqlalchemy import text
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE run_rows SET ingested_at = :t WHERE row_id = :rid"),
            {"t": ingested_at, "rid": row_id},
        )


# ---------------------------------------------------------------------------
# /calls tab=passed


def test_calls_tab_passed_returns_only_passed_rows(client, auth_headers):
    _push_default_config(client, auth_headers)
    r1 = _invoke(client, auth_headers, input="ok")
    # Force a fail by reconfiguring the heuristic to reject any output.
    fail_cfg = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "heuristics:\n  - id: len-tight\n    type: length\n    max: 1\n"
    )
    client.post("/v1/projects/default/config",
                json={"content": fail_cfg}, headers=auth_headers)
    r2 = _invoke(client, auth_headers, input="this-output-is-too-long")

    passed = client.get(
        "/v1/projects/default/calls?tab=passed&limit=50",
        headers=auth_headers,
    )
    assert passed.status_code == 200
    ids = {c["row_id"] for c in passed.json()["calls"]}
    assert r1.json()["row_id"] in ids
    assert r2.json()["row_id"] not in ids


def test_calls_tab_passed_complements_failures(client, auth_headers):
    """Recent tab is the union; passed + failures partition it.  This
    is the contract the UI relies on so the three tabs don't overlap."""
    _push_default_config(client, auth_headers)
    _invoke(client, auth_headers, input="x")

    recent   = client.get("/v1/projects/default/calls?tab=recent", headers=auth_headers).json()["calls"]
    passed   = client.get("/v1/projects/default/calls?tab=passed", headers=auth_headers).json()["calls"]
    failures = client.get("/v1/projects/default/calls?tab=failures", headers=auth_headers).json()["calls"]

    recent_ids   = {c["row_id"] for c in recent}
    partition    = {c["row_id"] for c in passed} | {c["row_id"] for c in failures}
    assert recent_ids == partition
    # The two halves are disjoint.
    assert not ({c["row_id"] for c in passed} & {c["row_id"] for c in failures})


# ---------------------------------------------------------------------------
# /calls ?from=&to=  time window


def test_calls_from_to_half_open_window(client, auth_headers):
    """``[from, to)`` filter narrows to one day; row at exactly ``to``
    is excluded (half-open semantics so adjacent windows don't double-count)."""
    _push_default_config(client, auth_headers)
    r1 = _invoke(client, auth_headers, input="day1")
    r2 = _invoke(client, auth_headers, input="day2")
    r3 = _invoke(client, auth_headers, input="day3")

    # Backdate the three rows to known timestamps.
    _set_row_ingested_at(client, r1.json()["row_id"], "2026-05-27T12:00:00+00:00")
    _set_row_ingested_at(client, r2.json()["row_id"], "2026-05-28T12:00:00+00:00")
    _set_row_ingested_at(client, r3.json()["row_id"], "2026-05-29T12:00:00+00:00")

    # Window covering only May 28 → only r2.
    r = client.get(
        "/v1/projects/default/calls"
        "?tab=recent&from=2026-05-28T00:00:00+00:00&to=2026-05-29T00:00:00+00:00",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    ids = {c["row_id"] for c in r.json()["calls"]}
    assert ids == {r2.json()["row_id"]}


def test_calls_from_to_combines_with_tab(client, auth_headers):
    """Window + tab=failures still filters by passed=0."""
    _push_default_config(client, auth_headers)
    r_pass = _invoke(client, auth_headers, input="ok")
    # Force fail mode.
    fail_cfg = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "heuristics: [{ id: x, type: length, max: 1 }]\n"
    )
    client.post("/v1/projects/default/config",
                json={"content": fail_cfg}, headers=auth_headers)
    r_fail = _invoke(client, auth_headers, input="long-enough-to-fail")

    _set_row_ingested_at(client, r_pass.json()["row_id"], "2026-05-28T12:00:00+00:00")
    _set_row_ingested_at(client, r_fail.json()["row_id"], "2026-05-28T13:00:00+00:00")

    r = client.get(
        "/v1/projects/default/calls"
        "?tab=failures&from=2026-05-28T00:00:00+00:00&to=2026-05-29T00:00:00+00:00",
        headers=auth_headers,
    )
    ids = {c["row_id"] for c in r.json()["calls"]}
    assert ids == {r_fail.json()["row_id"]}


# ---------------------------------------------------------------------------
# Detail endpoint pulls from detail_json for live calls


def test_detail_endpoint_surfaces_live_call_content(client, auth_headers):
    """Live calls store input/output/scores on ``run_rows.detail_json``
    (not ``runs.payload_json``).  The detail endpoint must surface
    them so /calls/ drill-down works on proxied rows."""
    _push_default_config(client, auth_headers)
    inv = _invoke(client, auth_headers, input="drill-me", expected="drill-me")
    body = inv.json()

    r = client.get(
        f"/v1/projects/default/calls/{body['run_id']}/{body['row_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["input"]    == "drill-me"
    assert d["expected"] == "drill-me"
    assert d["output"]   == "drill-me"
    assert d["provider"] == "mock"
    assert d["model"]    == "m"
    assert isinstance(d["scores"], list)
    assert d["passed"] is True


# ---------------------------------------------------------------------------
# /live/timeline


def test_live_timeline_empty_when_no_live_runs(client, auth_headers):
    r = client.get(
        "/v1/projects/default/live/timeline?days=30",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"entries": []}


def test_live_timeline_returns_runs_newest_first(client, auth_headers):
    _push_default_config(client, auth_headers)
    inv = _invoke(client, auth_headers, input="x")
    run_id = inv.json()["run_id"]
    _set_run_started_at(client, run_id, "2026-05-29T00:00:00+00:00")

    # Synthesise a second live run from yesterday so we can prove ordering.
    from sqlalchemy import text
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO runs(
                      run_id, project_id, project_name,
                      status, row_status, gate_status,
                      started_at, finished_at,
                      cost_usd, row_count, row_pass_count, row_fail_count,
                      payload_json, ingested_at, ingested_by, source)
                    VALUES (
                      'run_liveyesterdayxxx', (SELECT project_id FROM projects WHERE slug='default'),
                      'default', 'running', 'pending', 'pending',
                      '2026-05-28T00:00:00+00:00', NULL,
                      1.5, 100, 90, 10,
                      '{}', '2026-05-28T00:00:00+00:00', 'proxy', 'live')"""),
        )

    r = client.get(
        "/v1/projects/default/live/timeline?days=7",
        headers=auth_headers,
    )
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert len(entries) == 2
    # Newest first.
    assert entries[0]["run_id"] == run_id
    assert entries[1]["run_id"] == "run_liveyesterdayxxx"
    assert entries[1]["row_count"]      == 100
    assert entries[1]["row_pass_count"] == 90


def test_live_timeline_excludes_batch_runs(client, auth_headers):
    """Only ``source='live'`` runs land in the timeline — a CLI-pushed
    run should never show up here."""
    from sqlalchemy import text
    engine = client.app.state.engine
    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO runs(
                      run_id, project_id, project_name,
                      status, row_status, gate_status,
                      started_at,
                      cost_usd, row_count, row_pass_count, row_fail_count,
                      payload_json, ingested_at, ingested_by, source)
                    VALUES (
                      'run_clibatch01', (SELECT project_id FROM projects WHERE slug='default'),
                      'default', 'finished', 'finished', 'pass',
                      '2026-05-29T00:00:00+00:00',
                      0.5, 50, 50, 0,
                      '{}', '2026-05-29T00:00:00+00:00', 'cli', 'cli')"""),
        )
    r = client.get(
        "/v1/projects/default/live/timeline?days=30",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["entries"] == []


def test_live_timeline_respects_days_cap(client, auth_headers):
    """``days`` is validated 1..90 by FastAPI."""
    r = client.get(
        "/v1/projects/default/live/timeline?days=200",
        headers=auth_headers,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /live/aggregate


def test_live_aggregate_sums_across_runs(client, auth_headers):
    """SUM(row_*) + COUNT(runs) across the window."""
    from sqlalchemy import text
    engine = client.app.state.engine
    with engine.begin() as conn:
        for run_id, started, rc, rp in [
            ("run_livea", "2026-05-27T00:00:00+00:00", 100, 95),
            ("run_liveb", "2026-05-28T00:00:00+00:00", 200, 195),
            ("run_livec", "2026-05-29T00:00:00+00:00", 300, 270),
        ]:
            conn.execute(
                text("""INSERT INTO runs(
                          run_id, project_id, project_name,
                          status, row_status, gate_status,
                          started_at,
                          cost_usd, row_count, row_pass_count, row_fail_count,
                          payload_json, ingested_at, ingested_by, source)
                        VALUES (
                          :rid, (SELECT project_id FROM projects WHERE slug='default'),
                          'default', 'running', 'pending', 'pending',
                          :s,
                          :c, :rc, :rp, :rf,
                          '{}', :s, 'proxy', 'live')"""),
                {"rid": run_id, "s": started, "c": 2.0,
                 "rc": rc, "rp": rp, "rf": rc - rp},
            )

    # All-time aggregate covers all 3 runs.
    all_time = client.get(
        "/v1/projects/default/live/aggregate",
        headers=auth_headers,
    )
    assert all_time.status_code == 200
    a = all_time.json()
    assert a["row_count"]      == 600
    assert a["row_pass_count"] == 560
    assert a["row_fail_count"] == 40
    assert a["run_count"]      == 3

    # Window covers May 28 only.
    one_day = client.get(
        "/v1/projects/default/live/aggregate"
        "?from=2026-05-28T00:00:00+00:00&to=2026-05-29T00:00:00+00:00",
        headers=auth_headers,
    ).json()
    assert one_day["row_count"]      == 200
    assert one_day["row_pass_count"] == 195
    assert one_day["run_count"]      == 1


def test_live_aggregate_empty_window_returns_zero(client, auth_headers):
    """No live runs in the window → zero counters (not NULL)."""
    r = client.get(
        "/v1/projects/default/live/aggregate"
        "?from=2026-05-01T00:00:00+00:00&to=2026-05-02T00:00:00+00:00",
        headers=auth_headers,
    )
    assert r.status_code == 200
    a = r.json()
    assert a == {"row_count": 0, "row_pass_count": 0, "row_fail_count": 0,
                 "cost_usd": 0.0, "run_count": 0}


# ---------------------------------------------------------------------------
# Cross-tenant + auth


def test_live_timeline_cross_org_returns_404(
    client, auth_headers, make_org, make_member_token,
):
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    # ACME provisions a project.
    client.post(
        "/v1/projects",
        json={"slug": "secret", "name": "S"},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    r = client.get(
        "/v1/projects/secret/live/timeline",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404


def test_live_aggregate_unauth_returns_401(client):
    r = client.get("/v1/projects/default/live/aggregate")
    assert r.status_code == 401
