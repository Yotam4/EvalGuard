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
    chain.  ``prev_event_hash`` must be ``None`` (chain origin)."""
    _push_paid_config(client, auth_headers)
    r = _invoke(client, auth_headers)
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    events = client.get(
        f"/v1/projects/default/audit/events?run_id={run_id}",
        headers=auth_headers,
    ).json()["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"]            == "provider.called"
    assert ev["prev_event_hash"] is None
    assert ev["event_hash"]   # populated
    assert ev["actor_type"]      == "api_key"
    assert ev["actor_id"]   # the principal's key_id
    assert ev["payload"]["provider"] == "mock"
    assert ev["payload"]["model"]    == "m"
    # Cost flows through to the audit event so the reconciliation
    # use-case ("what did we charge today?") sums directly.
    assert ev["cost_usd"] == 0.10


def test_two_invokes_form_a_linear_chain(client, auth_headers):
    """Second event's ``prev_event_hash`` must equal first event's
    ``event_hash``.  This is the chain-fork-prevention invariant
    ``UNIQUE (run_id, prev_event_hash)`` exists to enforce."""
    _push_paid_config(client, auth_headers)
    r1 = _invoke(client, auth_headers, "a")
    r2 = _invoke(client, auth_headers, "b")
    assert r1.json()["run_id"] == r2.json()["run_id"]

    events = client.get(
        f"/v1/projects/default/audit/events?run_id={r1.json()['run_id']}",
        headers=auth_headers,
    ).json()["events"]
    assert len(events) == 2
    assert events[0]["prev_event_hash"] is None
    assert events[1]["prev_event_hash"] == events[0]["event_hash"]


def test_provider_failure_emits_provider_failed_event(client, auth_headers):
    """A failed upstream call still emits one chain-linked event,
    but with ``kind = provider.failed`` and the error message
    captured in the payload."""
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
    assert events[0]["kind"]               == "provider.failed"
    assert "simulated 500" in events[0]["payload"]["error"]


# ---------------------------------------------------------------------------
# Verify


def test_verify_clean_chain_returns_ok(client, auth_headers):
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

    engine = client.app.state.engine
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, event_id, event_json FROM event_rows "
                 "WHERE run_id = :rid ORDER BY id LIMIT 1 OFFSET 1"),
            {"rid": run_id},
        ).mappings().fetchone()
        # Mutate the event_json (the canonical form verify_chain
        # re-hashes) but leave event_hash + prev_event_hash alone.
        # This simulates after-the-fact tampering — exactly what
        # the chain exists to detect.
        tampered = json.loads(row["event_json"])
        tampered["payload"] = {"tampered": True}
        conn.execute(
            text("UPDATE event_rows SET event_json = :j WHERE id = :id"),
            {"j": json.dumps(tampered), "id": row["id"]},
        )

    v = client.get(
        f"/v1/projects/default/audit/verify?run_id={run_id}",
        headers=auth_headers,
    ).json()
    assert v["ok"] is False
    assert v["broken_at"] == row["event_id"]
    assert "event_hash mismatch" in v["reason"]


def test_verify_detects_prev_hash_tamper(client, auth_headers):
    """Bump one event's prev_event_hash to nonsense; verify must
    catch the chain-pointer mismatch."""
    from sqlalchemy import text
    _push_paid_config(client, auth_headers)
    _invoke(client, auth_headers, "a")
    r2 = _invoke(client, auth_headers, "b")
    run_id = r2.json()["run_id"]

    engine = client.app.state.engine
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, event_id, event_json FROM event_rows "
                 "WHERE run_id = :rid ORDER BY id LIMIT 1 OFFSET 1"),
            {"rid": run_id},
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
                 "prev_event_hash = :p, event_hash = :h WHERE id = :id"),
            {"j": json.dumps(tampered),
             "p": tampered["prev_event_hash"],
             "h": tampered["event_hash"],
             "id": row["id"]},
        )

    v = client.get(
        f"/v1/projects/default/audit/verify?run_id={run_id}",
        headers=auth_headers,
    ).json()
    assert v["ok"] is False
    assert v["broken_at"] == row["event_id"]
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
    r = client.get("/v1/projects/default/audit/events?run_id=run_anything")
    assert r.status_code == 401
    v = client.get("/v1/projects/default/audit/verify?run_id=run_anything")
    assert v.status_code == 401
