"""``GET /v1/projects/{slug}/alerts`` integration tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from evalguard_api.alerts import evaluate_alert_rule
from evalguard_api.config import Settings
from evalguard_api.db import make_engine
from evalguard_evaluators.notifiers.mock import MockNotifier


_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _push(client, headers):
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
    )
    client.post("/v1/projects/default/config",
                json={"content": cfg}, headers=headers)


def _seed_failing_alert(client, auth_headers, settings: Settings) -> dict:
    MockNotifier.reset()
    engine = make_engine(settings)
    _push(client, auth_headers)
    for _ in range(20):
        client.post(
            "/v1/projects/default/invoke",
            json={"input": "x"},
            headers=auth_headers,
        )
    with engine.connect() as conn:
        r = conn.execute(
            text("SELECT project_id, org_id FROM projects WHERE slug = 'default'"),
        ).mappings().fetchone()
    pid = r["project_id"]
    org_id = r["org_id"]
    # Force all rows into the window with passed=0.
    with engine.begin() as conn:
        ids = [
            row[0] for row in conn.execute(
                text("SELECT id FROM run_rows WHERE project_id = :pid ORDER BY id DESC LIMIT 20"),
                {"pid": pid},
            ).fetchall()
        ]
        for i, row_id in enumerate(ids):
            conn.execute(
                text("UPDATE run_rows SET passed = 0, ingested_at = :t WHERE id = :id"),
                {
                    "t":  (_NOW - timedelta(seconds=i * 30)).isoformat(),
                    "id": row_id,
                },
            )
    asyncio.run(evaluate_alert_rule(
        engine,
        project_id=pid, org_id=org_id,
        rule_id="route-test",
        rule_cfg={
            "window": "15m",
            "gate":   "pass_rate",
            "threshold": {"min": 0.9},
            "min_samples": 5,
            "notify":      [{"kind": "mock"}],
        },
        now=_NOW,
    ))
    engine.dispose()
    return {"project_id": pid, "org_id": org_id}


def test_get_alerts_returns_fired_row(client, auth_headers, settings):
    info = _seed_failing_alert(client, auth_headers, settings)
    r = client.get(
        "/v1/projects/default/alerts?limit=10",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["alerts"]) == 1
    a = body["alerts"][0]
    assert a["rule_id"] == "route-test"
    assert a["gate"] == "pass_rate"
    assert a["transition"] == "pass_to_fail"
    assert a["suppressed"] is False
    assert a["threshold"] == {"min": 0.9}
    assert a["observed_value"] == 0.0
    assert len(a["notify_results"]) == 1
    assert a["notify_results"][0]["kind"] == "mock"
    assert a["notify_results"][0]["ok"] is True


def test_get_alerts_rule_id_filter(client, auth_headers, settings):
    info = _seed_failing_alert(client, auth_headers, settings)
    # Non-matching rule_id filter → empty list.
    r = client.get(
        "/v1/projects/default/alerts?rule_id=other",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["alerts"] == []
    # Matching filter → the one row.
    r = client.get(
        "/v1/projects/default/alerts?rule_id=route-test",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert len(r.json()["alerts"]) == 1


def test_get_alerts_empty_when_nothing_fired(client, auth_headers):
    _push(client, auth_headers)
    r = client.get(
        "/v1/projects/default/alerts",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["alerts"] == []
