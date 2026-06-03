"""``/v1/projects/{slug}/audit/*`` — end-to-end coverage for the
proxy audit chain (PROXY-3.5).

The chain is invariant-heavy:
- Every invoke call emits exactly one event.
- Events form a linear chain: each ``prev_event_hash`` equals the
  immediately-previous event's ``event_hash``.
- Tampering breaks verify cleanly + names the failing event.
- Cross-org access 404s like every other project-scoped surface.
"""

from __future__ import annotations

import json


def _push_paid_config(client, headers):
    """Provider with a non-zero per-call cost so audit events carry
    a real ``cost_usd`` for the operator to reconcile.  Tests that
    care about cost surface assertions use this; the cheap version
    (echo, $0) is fine for chain-structural tests."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0, cost_per_call: 0.10 } }]\n"
    )
    r = client.post(
        "/v1/projects/default/config",
        json={"content": cfg},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text


def _invoke(client, headers, input_: str = "x"):
    return client.post(
        "/v1/projects/default/invoke",
        json={"input": input_},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Chain structure


def test_first_invoke_emits_one_event_with_no_prev(client, auth_headers):
    """The day's first call lazy-creates the live run AND opens the
    chain.  ``prev_event_hash`` must be ``None`` (chain origin).

    Test-quality round (Finding F8): assert ``actor_id`` equals the
    actual bootstrap admin key_id, not just truthy.  Anchors against
    a regression where the route accidentally hardcodes a wrong id.
    """
    from sqlalchemy import text as _text
    _push_paid_config(client, auth_headers)
    r = _invoke(client, auth_headers)
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    # Look up the actual bootstrap key_id so we can pin actor_id to it.
    # The lifespan creates ONE key for the default org named
    # "bootstrap (env)" (apps/api/evalguard_api/main.py:168).
    engine = client.app.state.engine
    with engine.connect() as conn:
        expected_key_id = conn.execute(
            _text("SELECT key_id FROM api_keys "
                  "WHERE name = 'bootstrap (env)' LIMIT 1"),
        ).scalar()
    assert expected_key_id is not None, (
        "bootstrap (env) key not found — fixture setup changed?"
    )

    events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}",
        headers=auth_headers,
    ).json()["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"]            == "provider.called"
    assert ev["prev_event_hash"] is None
    # event_hash must be a proper sha256 hex (64 chars) — a bug that
    # produced an empty or short hash would otherwise slip through.
    assert isinstance(ev["event_hash"], str) and len(ev["event_hash"]) == 64
    assert all(c in "0123456789abcdef" for c in ev["event_hash"])
    assert ev["actor_type"]      == "api_key"
    # Round-5 test-quality F8: pin the actual key_id, not just truthy.
    assert ev["actor_id"] == expected_key_id, (
        f"actor_id mismatch: event has {ev['actor_id']!r}, "
        f"bootstrap key is {expected_key_id!r}"
    )
    # Round-5 (Correctness K): actor_meta now carries scopes so an
    # auditor sees which permissions were in effect.  Bootstrap is
    # admin-scoped.
    assert ev["actor_meta"].get("scopes") == ["admin"]
    assert ev["payload"]["provider"] == "mock"
    assert ev["payload"]["model"]    == "m"
    # Cost flows through to the audit event so the reconciliation
    # use-case ("what did we charge today?") sums directly.
    assert ev["cost_usd"] == 0.10


def test_two_invokes_form_a_linear_chain(client, auth_headers):
    """Second event's ``prev_event_hash`` must equal first event's
    ``event_hash``.  This is the chain-fork-prevention invariant
    ``UNIQUE (run_id, prev_event_hash)`` exists to enforce.

    Test-quality round (Finding F3): assert event_hash is non-empty
    + properly-shaped on BOTH events.  Without this, a chain where
    one node had a blank hash could pass the chaining assertion
    trivially (``None == None`` style).
    """
    _push_paid_config(client, auth_headers)
    r1 = _invoke(client, auth_headers, "a")
    r2 = _invoke(client, auth_headers, "b")
    assert r1.json()["run_id"] == r2.json()["run_id"]

    events = client.get(
        f"/v1/projects/default/audit/events?run_id={r1.json()['run_id']}",
        headers=auth_headers,
    ).json()["events"]
    assert len(events) == 2
    # Both hashes are real sha256 hex (64 lowercase hex chars).
    for ev in events:
        h = ev["event_hash"]
        assert isinstance(h, str) and len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
    assert events[0]["prev_event_hash"] is None
    assert events[1]["prev_event_hash"] == events[0]["event_hash"]
    # And the two events DO NOT share an event_hash (would mean
    # the second insert duplicated the first).
    assert events[0]["event_hash"] != events[1]["event_hash"]


def test_provider_failure_emits_provider_failed_event(client, auth_headers):
    """A failed upstream call still emits one chain-linked event,
    but with ``kind = provider.failed`` and the error message
    captured in the payload.

    Test-quality round (Finding F4): also pin ``is_rate_limited``
    and ``is_cost_capped`` to False so a generic upstream-5xx
    failure can't be silently confused with a rate-limit or
    cost-cap path in the audit chain.
    """
    bad_cfg = (
        "version: 1\nproject: default\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0, fail_with: 'simulated 500' } }]\n"
    )
    client.post("/v1/projects/default/config",
                json={"content": bad_cfg}, headers=auth_headers)
    r = _invoke(client, auth_headers)
    assert r.status_code == 502, r.text   # upstream-5xx contract
    run_id = r.json()["run_id"]

    events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}",
        headers=auth_headers,
    ).json()["events"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert events[0]["kind"]            == "provider.failed"
    assert "simulated 500" in payload["error"]
    # Distinguish a generic upstream failure from rate-limit /
    # cost-cap paths — without these assertions a rate-limit
    # failure routed through the same code path would look
    # identical to this generic-failure case.
    assert payload["is_rate_limited"] is False
    assert payload["is_cost_capped"]  is False
    assert payload["passed"]          is False


# ---------------------------------------------------------------------------
# Verify


def test_verify_clean_chain_returns_ok(client, auth_headers):
    """Test-quality round (Finding F1): ``ok=True`` + ``events==4``
    is a weak anchor — verify could be a stub that always returns
    ok and the count would still match (since the count comes from
    ``len(events)``).  Strengthen by independently fetching the
    events and asserting the chain prev-links form an unbroken
    sequence — that's the structural invariant verify is
    SUPPOSED to enforce."""
    _push_paid_config(client, auth_headers)
    _invoke(client, auth_headers, "a")
    _invoke(client, auth_headers, "b")
    _invoke(client, auth_headers, "c")
    run_id = _invoke(client, auth_headers, "d").json()["run_id"]

    r = client.get(
        f"/v1/projects/default/audit/verify?run_id={run_id}",
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["events"] == 4
    assert body["broken_at"] is None
    assert body["reason"]

    # Independent structural check: the events list itself must
    # form a chain of length 4 with prev=None at the root and
    # each subsequent prev_event_hash == previous event_hash.
    # If verify were ever swapped for a stub that just returned
    # ``ok=True``, this still catches the regression.
    events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}",
        headers=auth_headers,
    ).json()["events"]
    assert len(events) == 4
    assert events[0]["prev_event_hash"] is None
    for i in range(1, 4):
        assert events[i]["prev_event_hash"] == events[i - 1]["event_hash"], (
            f"chain broken at event[{i}]: prev={events[i]['prev_event_hash']!r}, "
            f"expected={events[i - 1]['event_hash']!r}"
        )


def test_verify_detects_event_hash_tamper(client, auth_headers):
    """Mutate one event's payload AFTER it was hashed.  The
    recomputed hash differs from the stored hash → broken_at
    fires at the tampered event."""
    from sqlalchemy import text
    _push_paid_config(client, auth_headers)
    _invoke(client, auth_headers, "a")
    r2 = _invoke(client, auth_headers, "b")
    _invoke(client, auth_headers, "c")
    run_id = r2.json()["run_id"]

    # Test-quality round (Finding F2): identify the event to tamper
    # by ITS event_id via the public events API, not by raw-SQL
    # ORDER BY id OFFSET 1.  The OFFSET approach is fragile (any
    # ordering change or pre-existing fixture data shifts the
    # target) AND the previous assertion was self-referential
    # (``broken_at == row["event_id"]`` where row was the row we
    # tampered + the row whose id we asserted on).  Now: pick the
    # SECOND event from the public list, tamper it specifically,
    # and assert verify reports THAT event_id as broken_at.
    api_events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}",
        headers=auth_headers,
    ).json()["events"]
    assert len(api_events) == 3
    target_event_id = api_events[1]["event_id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        # Mutate the event_json (the canonical form verify_chain
        # re-hashes) but leave event_hash + prev_event_hash alone.
        # This simulates after-the-fact tampering — exactly what
        # the chain exists to detect.
        row = conn.execute(
            text("SELECT event_json FROM event_rows "
                 "WHERE event_id = :eid"),
            {"eid": target_event_id},
        ).mappings().fetchone()
        tampered = json.loads(row["event_json"])
        tampered["payload"] = {"tampered": True}
        conn.execute(
            text("UPDATE event_rows SET event_json = :j "
                 "WHERE event_id = :eid"),
            {"j": json.dumps(tampered), "eid": target_event_id},
        )

    v = client.get(
        f"/v1/projects/default/audit/verify?run_id={run_id}",
        headers=auth_headers,
    ).json()
    assert v["ok"] is False
    assert v["broken_at"] == target_event_id, (
        f"verify named the wrong event: got {v['broken_at']!r}, "
        f"expected the tampered event {target_event_id!r}"
    )
    assert "event_hash mismatch" in v["reason"]


def test_verify_detects_prev_hash_tamper(client, auth_headers):
    """Bump one event's prev_event_hash to nonsense; verify must
    catch the chain-pointer mismatch."""
    from sqlalchemy import text
    _push_paid_config(client, auth_headers)
    _invoke(client, auth_headers, "a")
    r2 = _invoke(client, auth_headers, "b")
    run_id = r2.json()["run_id"]

    # Test-quality round (F2): identify by event_id via the public
    # API, not OFFSET — same robustness reason as the event-hash
    # tamper test above.
    api_events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}",
        headers=auth_headers,
    ).json()["events"]
    target_event_id = api_events[1]["event_id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT event_json FROM event_rows WHERE event_id = :eid"),
            {"eid": target_event_id},
        ).mappings().fetchone()
        tampered = json.loads(row["event_json"])
        tampered["prev_event_hash"] = "0" * 64
        # Recompute event_hash so the per-event hash check passes —
        # forces verify to catch the prev-pointer mismatch
        # specifically (not the event-hash mismatch from above).
        from evalguard_evaluators.audit import hash_event
        tampered["event_hash"] = hash_event(tampered)
        conn.execute(
            text("UPDATE event_rows SET event_json = :j, "
                 "prev_event_hash = :p, event_hash = :h "
                 "WHERE event_id = :eid"),
            {"j": json.dumps(tampered),
             "p": tampered["prev_event_hash"],
             "h": tampered["event_hash"],
             "eid": target_event_id},
        )

    v = client.get(
        f"/v1/projects/default/audit/verify?run_id={run_id}",
        headers=auth_headers,
    ).json()
    assert v["ok"] is False
    assert v["broken_at"] == target_event_id, (
        f"verify named the wrong event: got {v['broken_at']!r}, "
        f"expected the tampered event {target_event_id!r}"
    )
    assert "prev_event_hash mismatch" in v["reason"]


# ---------------------------------------------------------------------------
# Tenant scoping


def test_audit_events_cross_org_returns_404(
    client, auth_headers, make_org, make_member_token,
):
    """A member of org-A asking for org-B's audit events must 404,
    not 403, and certainly not the bytes."""
    make_org("acme")
    member_acme    = make_member_token("org_acme",    name="a")
    member_default = make_member_token("org_default", name="d")

    # ACME provisions a project + emits one event.
    client.post(
        "/v1/projects",
        json={"slug": "secret", "name": "S"},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    cfg = (
        "version: 1\nproject: secret\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
    )
    client.post(
        "/v1/projects/secret/config",
        json={"content": cfg},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    inv = client.post(
        "/v1/projects/secret/invoke",
        json={"input": "x"},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    assert inv.status_code == 200
    run_id = inv.json()["run_id"]

    # Test-quality round (Finding F5): a sanity assertion that the
    # default-org member's token DOES authenticate (200 on its own
    # org's endpoints) before asserting 404 on the cross-org path.
    # Without this, a 404 could fire from auth failure misread as
    # anti-enumeration tenancy isolation.
    auth_sanity = client.get(
        "/v1/projects/default/calls?tab=recent&limit=1",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert auth_sanity.status_code == 200, (
        f"default-org member token failed auth on its OWN project "
        f"({auth_sanity.status_code}); the cross-org 404 below can't "
        f"be attributed to tenancy isolation"
    )

    # Default member knows the slug but must 404.
    r = client.get(
        f"/v1/projects/secret/audit/events?run_id={run_id}",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404
    v = client.get(
        f"/v1/projects/secret/audit/verify?run_id={run_id}",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert v.status_code == 404


def test_audit_events_wrong_project_for_run_returns_404(
    client, auth_headers,
):
    """The run belongs to the default project; querying it under
    a DIFFERENT project's slug must 404 (anti-enumeration)."""
    # Provision a sibling project.
    other = client.post(
        "/v1/projects",
        json={"slug": "other-proj", "name": "Other"},
        headers=auth_headers,
    )
    assert other.status_code == 201

    _push_paid_config(client, auth_headers)
    run_id = _invoke(client, auth_headers).json()["run_id"]

    # Wrong project slug → 404.
    r = client.get(
        f"/v1/projects/other-proj/audit/events?run_id={run_id}",
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_audit_events_unauth_returns_401(client):
    """Test-quality round (Finding F6): use a real-shape run_id so
    a future route-level format validator (UUID-shaped, prefix-
    pattern, etc.) can't 422 the request before the auth check
    fires.  ``run_live_<16 hex>`` matches the proxy's deterministic
    id format from ``live.live_run_id``."""
    well_formed = "run_live0123456789abcdef"
    r = client.get(f"/v1/projects/default/audit/events?run_id={well_formed}")
    assert r.status_code == 401, (
        f"got {r.status_code} instead of 401 — auth must fire "
        f"before any other gate.  Body: {r.text[:200]}"
    )
    v = client.get(f"/v1/projects/default/audit/verify?run_id={well_formed}")
    assert v.status_code == 401


def test_chain_root_partial_unique_index_blocks_double_first_event(client, auth_headers):
    """Round-5 ultra-review (Agent-1 D + G): the original
    ``UNIQUE (run_id, prev_event_hash)`` constraint did NOT prevent
    two events from sharing ``(run_id, NULL)`` — ANSI SQL treats
    NULLs as distinct in UNIQUE constraints, so Postgres + SQLite
    both accepted a forked chain root.  The partial unique index
    added in this round (``WHERE prev_event_hash IS NULL``) closes
    the hole.

    Reproduce the race directly: emit one real event via /invoke
    (which lands the first NULL-prev row), then attempt to insert
    a SECOND row with the same run_id + prev_event_hash=NULL via
    raw SQL.  The second INSERT must fail with IntegrityError; the
    fact that it would have succeeded before is the bug this fix
    closes."""
    from sqlalchemy import text as _text
    from sqlalchemy.exc import IntegrityError

    _push_paid_config(client, auth_headers)
    r = _invoke(client, auth_headers, "first")
    run_id = r.json()["run_id"]

    engine = client.app.state.engine
    raised = False
    try:
        with engine.begin() as conn:
            conn.execute(
                _text("""INSERT INTO event_rows(
                          event_id, run_id, project_id,
                          kind, actor_id, actor_type,
                          prev_event_hash, event_hash,
                          event_json, ingested_at)
                        VALUES (
                          'rogue_root_event_x', :rid,
                          (SELECT project_id FROM projects WHERE slug='default'),
                          'provider.called', 'attacker', 'api_key',
                          NULL, 'rogue_hash_y',
                          '{}', '2026-06-02T05:00:00')"""),
                {"rid": run_id},
            )
    except IntegrityError:
        raised = True

    assert raised, (
        "partial UNIQUE INDEX on (run_id) WHERE prev_event_hash IS NULL "
        "did not fire — chain-fork at the root is possible"
    )
    # Sanity: the legitimate first event is still present.  Only ONE
    # NULL-prev row exists for this run after the test.
    with engine.connect() as conn:
        null_prev_count = conn.execute(
            _text("SELECT COUNT(*) FROM event_rows "
                  "WHERE run_id = :rid AND prev_event_hash IS NULL"),
            {"rid": run_id},
        ).scalar_one()
    assert null_prev_count == 1, (
        f"expected exactly one chain root after the rogue insert; "
        f"got {null_prev_count}"
    )


# ---------------------------------------------------------------------------
# Corrupt rows + verify cap + retry exhaustion
# Round-7 review-pass: cover the three remaining audit edges that
# the round-5 ultra-review fixes added (corrupt_rows, 413-on-verify,
# RuntimeError on retry exhaustion) but had no end-to-end tests for.


def test_corrupt_event_json_surfaces_via_corrupt_rows_and_fails_verify(
    client, auth_headers,
):
    """A row whose ``event_json`` column won't JSON-decode must be:
    1. Counted in ``/audit/events`` ``corrupt_rows`` (not silently
       dropped — operators have to KNOW the chain has gaps).
    2. Surfaced as ``ok=False`` from ``/audit/verify`` with a reason
       that names the corruption (so the operator doesn't read an
       ``ok=True`` on the visible prefix and assume the chain is clean).
    """
    from sqlalchemy import text
    _push_paid_config(client, auth_headers)
    _invoke(client, auth_headers, "good1")
    r = _invoke(client, auth_headers, "good2")
    run_id = r.json()["run_id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        project_id = conn.execute(
            text("SELECT project_id FROM runs WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar()
        # Inject a row whose event_json is not valid JSON.  Use a
        # unique prev_event_hash so the (run_id, prev_event_hash)
        # UNIQUE doesn't fire.  This is the exact malformed-row
        # shape ``list_events_for_run`` catches in its except branch.
        conn.execute(
            text("""INSERT INTO event_rows(
                      event_id, run_id, project_id,
                      kind, actor_id, actor_type,
                      prev_event_hash, event_hash,
                      event_json, ingested_at)
                    VALUES (
                      'evt_corrupt_x', :rid, :pid,
                      'provider.called', 'k', 'api_key',
                      'unique_prev_for_corrupt_row', 'fake_event_hash',
                      'this is not json', '2026-06-02T05:00:00')"""),
            {"rid": run_id, "pid": project_id},
        )

    events_body = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}",
        headers=auth_headers,
    ).json()
    assert events_body["corrupt_rows"] == 1, (
        f"expected 1 corrupt row, got {events_body['corrupt_rows']}"
    )
    # total counts ALL rows (corrupt + clean); the 2 real events
    # plus the malformed one we just injected.
    assert events_body["total"] == 3
    # The DECODABLE prefix is what landed in ``events``.
    assert events_body["count"] == 2

    v = client.get(
        f"/v1/projects/default/audit/verify?run_id={run_id}",
        headers=auth_headers,
    ).json()
    assert v["ok"] is False
    # ``events`` on the verify response is the FULL row count (per
    # the round-7 fix in audit.py), not just the decodable prefix.
    assert v["events"] == 3
    assert "corrupt" in v["reason"].lower()


def test_verify_refuses_chains_beyond_events_max_with_413(
    client, auth_headers,
):
    """``/audit/verify`` MUST refuse rather than silently truncate
    when the chain exceeds the per-page cap.  Reporting ``ok=True``
    on the first 500 events of a longer chain would mislead the
    operator into believing the whole chain is intact.  ``/audit/
    events`` keeps its silent cap but flips ``truncated=True`` so the
    caller can iterate."""
    from sqlalchemy import text
    _push_paid_config(client, auth_headers)
    r = _invoke(client, auth_headers, "seed")
    run_id = r.json()["run_id"]

    engine = client.app.state.engine
    # The route's cap is 500.  Inject 500 dummy rows past the 1 real
    # one to push the total to 501.  Each row needs a unique
    # ``prev_event_hash`` per the composite UNIQUE; the actual chain
    # links don't matter here — we're testing the count gate, not
    # verify's hash walk.
    with engine.begin() as conn:
        project_id = conn.execute(
            text("SELECT project_id FROM runs WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar()
        rows = [
            {
                "eid": f"evt_dummy_{i}",
                "rid": run_id,
                "pid": project_id,
                "prev": f"dummy_prev_{i}",
                "hash": f"dummy_hash_{i}",
                "json_blob": '{"placeholder": true}',
            }
            for i in range(500)
        ]
        conn.execute(
            text("""INSERT INTO event_rows(
                      event_id, run_id, project_id,
                      kind, actor_id, actor_type,
                      prev_event_hash, event_hash,
                      event_json, ingested_at)
                    VALUES (
                      :eid, :rid, :pid,
                      'provider.called', 'k', 'api_key',
                      :prev, :hash,
                      :json_blob, '2026-06-02T05:00:00')"""),
            rows,
        )

    v = client.get(
        f"/v1/projects/default/audit/verify?run_id={run_id}",
        headers=auth_headers,
    )
    assert v.status_code == 413, (
        f"expected 413 (chain exceeds cap); got {v.status_code}: {v.text[:200]}"
    )
    assert "cap" in v.json()["detail"].lower()

    # The LIST endpoint silently caps + signals truncation rather
    # than refusing — operators iterating chains in pages need this
    # to work (cursor pagination is the future fix).
    events_body = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}&limit=500",
        headers=auth_headers,
    ).json()
    assert events_body["count"] == 500
    assert events_body["total"] == 501
    assert events_body["truncated"] is True


def test_emit_event_runtime_error_on_retry_exhaustion(
    client, auth_headers, monkeypatch,
):
    """If every INSERT loses the chain-tip race for the bounded
    retry budget, ``emit_event`` must surface a ``RuntimeError``
    rather than silently dropping the event.  This is the only
    audited fail-loud path for an exhausted-retry condition; without
    a regression here a future refactor could silently swallow the
    failure (returning None or last_err.detail) and the chain would
    stop growing without anyone noticing.

    Forces exhaustion by monkeypatching ``chain_tip_for_run`` to
    always return ``None``.  After the first legitimate event lands
    (prev=NULL), the partial unique index ``WHERE prev_event_hash IS
    NULL`` rejects every subsequent NULL-prev INSERT → IntegrityError
    on each retry → loop exits via ``RuntimeError``."""
    import pytest
    from sqlalchemy import text as _text
    from evalguard_api import audit_persistence

    _push_paid_config(client, auth_headers)
    r = _invoke(client, auth_headers, "seed")
    run_id = r.json()["run_id"]

    engine = client.app.state.engine
    with engine.connect() as conn:
        project_id = conn.execute(
            _text("SELECT project_id FROM runs WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar()

    # Patch the chain-tip read so every retry sees the same stale
    # (None) tip → every INSERT lands a duplicate NULL-prev row →
    # partial unique index slams them all.
    monkeypatch.setattr(
        audit_persistence, "chain_tip_for_run", lambda c, r: None,
    )

    with engine.begin() as conn:
        with pytest.raises(RuntimeError, match=r"lost \d+ retries"):
            audit_persistence.emit_event(
                conn,
                kind="provider.called",
                run_id=run_id,
                project_id=project_id,
                actor_id="bootstrap",
                actor_type="api_key",
            )

    # Sanity: no spurious row landed.  The one legitimate event
    # from _invoke + nothing from the exhausted retries.
    with engine.connect() as conn:
        total = conn.execute(
            _text("SELECT COUNT(*) FROM event_rows WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar_one()
    assert total == 1, (
        f"emit_event leaked {total - 1} row(s) past the RuntimeError; "
        "the SAVEPOINT must roll back every failed INSERT"
    )


def test_emit_event_rejects_mismatched_project_id(client, auth_headers):
    """Round-5 ultra-review (Correctness J + Security G): the
    ``project_id`` argument to ``emit_event`` must match the
    ``runs.project_id`` of the supplied ``run_id``.  Without this
    runtime check, a buggy caller could write events scoped to the
    WRONG tenant — RLS on event_rows would then grant the wrong
    org visibility while hiding the events from the rightful owner.

    Reproduces by calling ``emit_event`` directly (the only way to
    trigger the mismatch — invoke.py always resolves the project
    correctly).  Asserts ValueError fires BEFORE any chain row
    lands."""
    import pytest
    from sqlalchemy import text as _text
    from evalguard_api.audit_persistence import emit_event

    # First do a real invoke to lazy-create the live run.
    _push_paid_config(client, auth_headers)
    r = _invoke(client, auth_headers, "seed")
    run_id = r.json()["run_id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        # Count audit rows BEFORE the rogue call so we can assert
        # nothing landed during the rejected attempt.
        before = conn.execute(
            _text("SELECT COUNT(*) FROM event_rows WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar_one()

        with pytest.raises(ValueError, match="project_id mismatch"):
            emit_event(
                conn,
                kind="provider.called",
                run_id=run_id,
                project_id="proj_TOTALLY_WRONG",   # intentionally wrong
                actor_id="some_key",
                actor_type="api_key",
            )

        # Fail-closed: no row landed.
        after = conn.execute(
            _text("SELECT COUNT(*) FROM event_rows WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar_one()
    assert before == after, (
        f"emit_event wrote {after - before} row(s) despite raising — "
        "the mismatch check must fire BEFORE the chain insert"
    )
