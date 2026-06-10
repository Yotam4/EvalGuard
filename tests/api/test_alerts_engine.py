"""End-to-end tests for ``evaluate_alert_rule`` / ``evaluate_all_alert_rules``.

These exercise the real DB + the real notifier registry; they pass
a fixed ``now`` to keep window math deterministic.  ``MockNotifier``
catches notifier dispatches so we don't open an outbound HTTP
connection.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from evalguard_api.alerts import (
    evaluate_alert_rule, evaluate_all_alert_rules,
)
from evalguard_api.config import Settings
from evalguard_api.db import make_engine
from evalguard_evaluators.notifiers.mock import MockNotifier


_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def engine(settings: Settings):
    e = make_engine(settings)
    yield e
    e.dispose()


@pytest.fixture(autouse=True)
def reset_mock_notifier():
    MockNotifier.reset()
    yield
    MockNotifier.reset()


@pytest.fixture
def seeded_project(client, auth_headers, engine):
    """Push one config + invoke enough times to give the alert
    engine real ``run_rows`` to aggregate over.  Returns
    ``(project_id, org_id)``."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
    )
    client.post(
        "/v1/projects/default/config", json={"content": cfg},
        headers=auth_headers,
    )
    # 20 passing rows.
    for _ in range(20):
        client.post(
            "/v1/projects/default/invoke",
            json={"input": "ok"},
            headers=auth_headers,
        )
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT p.project_id, p.org_id FROM projects p WHERE p.slug = 'default'"),
        ).mappings().fetchone()
    return {"project_id": row["project_id"], "org_id": row["org_id"]}


def _backdate_rows(engine, project_id: str, *, passed: int, n: int, base: datetime, span_seconds: int = 600) -> None:
    """Tag the last n ``run_rows`` (newest first) so they fall STRICTLY
    INSIDE the alert window with the configured ``passed`` value.

    The window query uses ``ingested_at < window_end`` (half-open),
    so we offset the first row by 1 second to keep it inside the
    window.
    """
    step = max(1, span_seconds // max(1, n))
    with engine.begin() as conn:
        ids = [
            r[0] for r in conn.execute(
                text("SELECT id FROM run_rows WHERE project_id = :pid ORDER BY id DESC LIMIT :n"),
                {"pid": project_id, "n": n},
            ).fetchall()
        ]
        for i, row_id in enumerate(ids):
            ts = (base - timedelta(seconds=1 + i * step)).isoformat()
            conn.execute(
                text("UPDATE run_rows SET ingested_at = :t, passed = :p WHERE id = :id"),
                {"t": ts, "p": passed, "id": row_id},
            )


def _rule(notify_cfg=None, **overrides):
    rule = {
        "window": "15m",
        "gate": "pass_rate",
        "threshold": {"min": 0.9},
        "min_samples": 5,
        "suppress_secs": 600,
        "notify": [notify_cfg or {"kind": "mock"}],
    }
    rule.update(overrides)
    return rule


# ---------------------------------------------------------------------------
# Skip path — window too quiet


def test_alert_skips_when_below_min_samples(seeded_project, engine):
    """A 15-minute window with fewer than ``min_samples`` rows must
    NOT change state — quiet periods would otherwise trip false
    failure alerts."""
    # All 20 rows are at ingestion time (right around _NOW or later);
    # the window we evaluate looks back from a DIFFERENT moment so
    # the rows fall outside.
    older = _NOW - timedelta(hours=4)
    outcome = asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="quiet-period",
        rule_cfg=_rule(min_samples=10),
        now=older,
    ))
    assert outcome.fired is False
    assert outcome.resolved is False
    assert "below min_samples" in outcome.decision.reason


# ---------------------------------------------------------------------------
# Fire / suppress / resolve loop


def test_alert_fires_on_pass_to_fail(seeded_project, engine):
    """Backdate 20 failing rows into the window: first eval flips
    pass→fail, fires notifier, records the alert."""
    _backdate_rows(
        engine, seeded_project["project_id"],
        passed=0, n=20, base=_NOW,
    )
    outcome = asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="drop",
        rule_cfg=_rule(),
        now=_NOW,
    ))
    assert outcome.fired is True
    assert outcome.decision.transition == "pass_to_fail"
    assert outcome.window.row_count == 20
    assert outcome.window.observed_value == 0.0
    assert outcome.alert_id is not None
    assert len(MockNotifier.sent) == 1
    sent = MockNotifier.sent[0]
    assert sent.rule_id == "drop"
    assert sent.transition == "pass_to_fail"
    assert sent.observed_value == 0.0


def test_alert_suppresses_within_window_then_refires(seeded_project, engine):
    """First eval: fires (pass→fail).  Second eval 60s later while
    rows still fail: suppressed (no notifier).  Third eval 700s
    after the first fire: notifier re-fires."""
    _backdate_rows(
        engine, seeded_project["project_id"],
        passed=0, n=20, base=_NOW,
    )
    # First eval: fires.
    asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="rule-1",
        rule_cfg=_rule(suppress_secs=600),
        now=_NOW,
    ))
    assert len(MockNotifier.sent) == 1

    # Re-backdate the rows so the 60-seconds-later window still
    # contains them.
    _backdate_rows(
        engine, seeded_project["project_id"],
        passed=0, n=20, base=_NOW + timedelta(seconds=60),
    )
    outcome2 = asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="rule-1",
        rule_cfg=_rule(suppress_secs=600),
        now=_NOW + timedelta(seconds=60),
    ))
    assert outcome2.suppressed is True
    assert outcome2.fired is False
    assert len(MockNotifier.sent) == 1  # no extra notifier fire

    # Third eval at 700s after the first fire: re-fires.
    _backdate_rows(
        engine, seeded_project["project_id"],
        passed=0, n=20, base=_NOW + timedelta(seconds=700),
    )
    outcome3 = asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="rule-1",
        rule_cfg=_rule(suppress_secs=600),
        now=_NOW + timedelta(seconds=700),
    ))
    assert outcome3.fired is True
    assert len(MockNotifier.sent) == 2


def test_alert_resolves_on_fail_to_pass(seeded_project, engine):
    """Fire once, then backdate passing rows: next eval resolves."""
    _backdate_rows(
        engine, seeded_project["project_id"],
        passed=0, n=20, base=_NOW,
    )
    asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="rule-resolve",
        rule_cfg=_rule(),
        now=_NOW,
    ))
    MockNotifier.reset()
    _backdate_rows(
        engine, seeded_project["project_id"],
        passed=1, n=20, base=_NOW + timedelta(minutes=2),
    )
    outcome = asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="rule-resolve",
        rule_cfg=_rule(),
        now=_NOW + timedelta(minutes=2),
    ))
    assert outcome.resolved is True
    assert outcome.fired is False
    assert len(MockNotifier.sent) == 1
    assert MockNotifier.sent[0].transition == "fail_to_pass"


# ---------------------------------------------------------------------------
# Persistence — the ``alerts`` table is the alert engine's append-only log


def test_alert_fire_persists_to_alerts_table(seeded_project, engine):
    _backdate_rows(
        engine, seeded_project["project_id"],
        passed=0, n=20, base=_NOW,
    )
    outcome = asyncio.run(evaluate_alert_rule(
        engine,
        project_id=seeded_project["project_id"],
        org_id=seeded_project["org_id"],
        rule_id="persisted",
        rule_cfg=_rule(),
        now=_NOW,
    ))
    assert outcome.fired is True
    with engine.connect() as conn:
        row = conn.execute(
            text("""SELECT rule_id, transition, suppressed, gate, observed_value
                    FROM alerts WHERE project_id = :pid"""),
            {"pid": seeded_project["project_id"]},
        ).mappings().fetchone()
    assert row is not None
    assert row["rule_id"] == "persisted"
    assert row["transition"] == "pass_to_fail"
    assert int(row["suppressed"]) == 0
    assert row["gate"] == "pass_rate"
    assert float(row["observed_value"]) == 0.0


# ---------------------------------------------------------------------------
# Cron-level entry: ``evaluate_all_alert_rules``


def test_evaluate_all_alert_rules_picks_up_project_config(
    client, auth_headers, engine,
):
    """The cron entry pulls every project's latest config and runs
    every rule under ``alerts:``."""
    cfg = (
        "version: 1\n"
        "project: default\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "alerts:\n"
        "  drop-1:\n"
        "    window: 15m\n"
        "    gate: pass_rate\n"
        "    threshold: { min: 0.9 }\n"
        "    min_samples: 5\n"
        "    notify:\n"
        "      - kind: mock\n"
    )
    client.post(
        "/v1/projects/default/config", json={"content": cfg},
        headers=auth_headers,
    )
    for _ in range(20):
        client.post(
            "/v1/projects/default/invoke",
            json={"input": "ok"},
            headers=auth_headers,
        )

    with engine.connect() as conn:
        pid = conn.execute(
            text("SELECT project_id FROM projects WHERE slug = 'default'"),
        ).scalar()

    # Make the window look failing.
    _backdate_rows(engine, pid, passed=0, n=20, base=_NOW)

    outcomes = asyncio.run(evaluate_all_alert_rules(engine, now=_NOW))
    assert len(outcomes) >= 1
    fired = [o for o in outcomes if o.rule_id == "drop-1"]
    assert len(fired) == 1
    assert fired[0].fired is True
    assert len(MockNotifier.sent) == 1
