"""Rolling-window alert engine.

Slice C.  Periodic (cron) evaluation of project alert rules against
the same ``run_rows`` table the /calls/ endpoints read from.  When a
window-aggregate crosses the configured threshold, the engine emits
an ``alert.fired`` audit event AND dispatches every configured
notifier; on the inverse transition (failing → passing) it emits
``alert.resolved`` and notifies (if the notifier opts in via
``notify_on_resolve``).

The engine deliberately separates pure logic from I/O:

- ``state_transition`` is a pure function — given the previous state,
  the observed gate result, and the current time, it returns the
  next state + whether to fire / resolve / suppress.  Tested in
  isolation; no DB, no time.time().
- ``evaluate_alert_rule`` is the I/O wrapper that reads the window
  from the DB, calls ``state_transition``, persists state changes,
  inserts an ``alerts`` history row, and dispatches notifiers.
- ``evaluate_all_alert_rules`` is the cron entry — enumerates
  projects, loads each project's latest config, evaluates every
  rule under ``alerts:``.

The same engine is callable from the Arq worker (cron) AND from a
test fixture (synchronous).  Determinism: every function that needs
"now" accepts a ``now: datetime | None`` parameter (default
``datetime.utcnow()``) so tests pin the clock without monkeypatching.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from evalguard_api.audit_persistence import emit_event
from evalguard_api.db import apply_rls_context


logger = logging.getLogger("evalguard.api.alerts")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


AlertState = Literal["pass", "fail", "suppressed"]
Transition = Literal[
    "no_change", "pass_to_fail", "fail_to_pass",
    "fail_to_suppressed", "suppressed_to_pass",
]


@dataclass(frozen=True)
class WindowResult:
    """One window query's output before the threshold compares."""

    row_count: int
    observed_value: float | None
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True)
class StateDecision:
    """Pure-logic verdict the state machine produces.

    The engine's I/O wrapper acts on ``new_state`` (UPSERT into
    ``alert_state``) and ``fire`` / ``resolve`` (insert into
    ``alerts``, emit audit event, dispatch notifiers).
    """

    new_state: AlertState
    transition: Transition
    fire: bool       # emit alert.fired + dispatch notifiers
    resolve: bool    # emit alert.resolved + (optionally) notify
    suppress: bool   # record a suppressed history row, no notifier
    reason: str


# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------


_WINDOW_RE = re.compile(r"^(?P<n>[0-9]+)(?P<unit>[mhd])$")


def parse_window(window: str) -> timedelta:
    """``"15m"`` → ``timedelta(minutes=15)``; ``"24h"``, ``"7d"`` similarly.

    The schema validates the regex too; this is the engine-side
    converter.  Raises ``ValueError`` on unparseable input so a
    misconfigured rule fails loudly at evaluation time.
    """
    m = _WINDOW_RE.match(window)
    if m is None:
        raise ValueError(f"unparseable alert window: {window!r}")
    n = int(m.group("n"))
    if n <= 0:
        raise ValueError(f"alert window must be positive: {window!r}")
    unit = m.group("unit")
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    raise ValueError(f"unknown window unit: {unit!r}")


# ---------------------------------------------------------------------------
# Pure-logic state machine
# ---------------------------------------------------------------------------


def evaluate_threshold(
    observed_value: float | None,
    threshold: dict[str, Any],
) -> bool:
    """True when the observed value is INSIDE the passing band.

    ``threshold.min`` ⇒ observed_value must be ≥ min.
    ``threshold.max`` ⇒ observed_value must be ≤ max.
    Both ⇒ observed_value must be in [min, max].
    Neither ⇒ vacuous pass (the rule isn't really configured).
    A None observed value (no samples) also vacuously passes — the
    ``min_samples`` gate is supposed to catch the empty-window case
    before this function runs.
    """
    if observed_value is None:
        return True
    min_v = threshold.get("min")
    max_v = threshold.get("max")
    if isinstance(min_v, (int, float)) and observed_value < float(min_v):
        return False
    if isinstance(max_v, (int, float)) and observed_value > float(max_v):
        return False
    return True


def state_transition(
    previous_state: AlertState,
    gate_passes: bool,
    last_fire_at: datetime | None,
    now: datetime,
    suppress_secs: int,
) -> StateDecision:
    """Drive the (pass | fail | suppressed) state machine.

    Rules:
    - pass → fail: fire notifier, transition to fail.
    - fail → pass: emit resolve, transition to pass.
    - fail → fail within suppress window: stay fail, record
      suppressed history row, no notifier.
    - fail → fail OUTSIDE suppress window: re-fire notifier (the
      condition is still bad and the operator hasn't seen recent
      noise).
    - suppressed → fail: re-fire when suppression expires.
    - pass → pass: no_change, no record.
    """
    if previous_state == "pass":
        if gate_passes:
            return StateDecision(
                new_state="pass", transition="no_change",
                fire=False, resolve=False, suppress=False,
                reason="still passing",
            )
        return StateDecision(
            new_state="fail", transition="pass_to_fail",
            fire=True, resolve=False, suppress=False,
            reason="threshold breached",
        )

    # previous_state in {"fail", "suppressed"}
    if gate_passes:
        return StateDecision(
            new_state="pass",
            transition=(
                "suppressed_to_pass" if previous_state == "suppressed"
                else "fail_to_pass"
            ),
            fire=False, resolve=True, suppress=False,
            reason="recovered",
        )

    # still failing
    if last_fire_at is not None and suppress_secs > 0:
        elapsed = (now - last_fire_at).total_seconds()
        if elapsed < suppress_secs:
            return StateDecision(
                new_state="suppressed", transition="fail_to_suppressed",
                fire=False, resolve=False, suppress=True,
                reason=f"suppressed for {int(suppress_secs - elapsed)}s more",
            )
    # Either no prior fire OR suppression has elapsed: re-fire.
    return StateDecision(
        new_state="fail", transition="pass_to_fail",
        fire=True, resolve=False, suppress=False,
        reason="still failing after suppression",
    )


# ---------------------------------------------------------------------------
# Window query — runs on the same idx_run_rows_calls index the
# /live/aggregate endpoint reads from.
# ---------------------------------------------------------------------------


_PASS_RATE_SQL = """\
SELECT COUNT(*) AS row_count,
       COALESCE(SUM(passed), 0) AS pass_count
  FROM run_rows
 WHERE project_id  = :pid
   AND ingested_at >= :from_ts
   AND ingested_at <  :to_ts
"""


def compute_window(
    conn: Connection,
    project_id: str,
    gate: str,
    *,
    window: timedelta,
    now: datetime,
) -> WindowResult:
    """Read the rolling-window aggregate for one rule's gate."""
    window_end = now
    window_start = now - window
    row = conn.execute(
        text(_PASS_RATE_SQL),
        {
            "pid":     project_id,
            "from_ts": window_start.isoformat(),
            "to_ts":   window_end.isoformat(),
        },
    ).mappings().fetchone()
    row_count = int(row["row_count"] or 0)
    pass_count = int(row["pass_count"] or 0)
    if gate == "pass_rate":
        observed = (pass_count / row_count) if row_count > 0 else None
    else:
        # Schema enum currently lists only pass_rate; future gates
        # plug in here.
        observed = None
    return WindowResult(
        row_count=row_count, observed_value=observed,
        window_start=window_start, window_end=window_end,
    )


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _now_iso(d: datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Strip trailing 'Z' before fromisoformat (Python ≤3.10
        # compatibility — we run on 3.11 but the production fleet
        # may not).
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _load_state(
    conn: Connection, project_id: str, rule_id: str,
) -> tuple[AlertState, datetime | None]:
    row = conn.execute(
        text("""SELECT state, last_fire_at FROM alert_state
                WHERE project_id = :pid AND rule_id = :rid"""),
        {"pid": project_id, "rid": rule_id},
    ).mappings().fetchone()
    if row is None:
        return "pass", None
    state = row["state"]
    if state not in ("pass", "fail", "suppressed"):
        state = "pass"
    return state, _parse_iso(row["last_fire_at"])  # type: ignore[return-value]


def _upsert_state(
    conn: Connection,
    project_id: str,
    rule_id: str,
    *,
    state: AlertState,
    now: datetime,
    last_fire_at: datetime | None,
) -> None:
    now_s = _now_iso(now)
    last_fire_s = _now_iso(last_fire_at) if last_fire_at else None
    exists = conn.execute(
        text("""SELECT 1 FROM alert_state
                WHERE project_id = :pid AND rule_id = :rid"""),
        {"pid": project_id, "rid": rule_id},
    ).first()
    if exists is None:
        conn.execute(
            text("""INSERT INTO alert_state
                    (project_id, rule_id, state,
                     last_transition_at, last_fire_at, last_check_at)
                    VALUES (:pid, :rid, :state, :ts, :lf, :now)"""),
            {
                "pid":   project_id, "rid": rule_id, "state": state,
                "ts":    now_s, "lf": last_fire_s, "now": now_s,
            },
        )
    else:
        conn.execute(
            text("""UPDATE alert_state
                    SET state              = :state,
                        last_transition_at = :ts,
                        last_fire_at       = :lf,
                        last_check_at      = :now
                    WHERE project_id = :pid AND rule_id = :rid"""),
            {
                "pid":   project_id, "rid": rule_id, "state": state,
                "ts":    now_s, "lf": last_fire_s, "now": now_s,
            },
        )


def _insert_alert(
    conn: Connection,
    *,
    project_id: str,
    rule_id: str,
    fired_at: datetime,
    window: WindowResult,
    gate: str,
    threshold: dict[str, Any],
    transition: Transition,
    suppressed: bool,
    notify_results: list[dict[str, Any]],
) -> int:
    res = conn.execute(
        text("""INSERT INTO alerts
                (project_id, rule_id, fired_at,
                 window_start, window_end, gate,
                 observed_value, threshold_json, transition,
                 suppressed, notify_results_json)
                VALUES (:pid, :rid, :fa, :ws, :we, :g,
                        :ov, :thr, :tr, :sup, :nr)"""),
        {
            "pid":  project_id, "rid": rule_id,
            "fa":   _now_iso(fired_at),
            "ws":   _now_iso(window.window_start),
            "we":   _now_iso(window.window_end),
            "g":    gate,
            "ov":   window.observed_value,
            "thr":  json.dumps(threshold),
            "tr":   transition,
            "sup":  1 if suppressed else 0,
            "nr":   json.dumps(notify_results),
        },
    )
    pk = res.lastrowid if hasattr(res, "lastrowid") else None
    return int(pk) if pk is not None else 0


# ---------------------------------------------------------------------------
# Notifier dispatch
# ---------------------------------------------------------------------------


async def _dispatch_notifiers(
    notify_specs: Iterable[dict[str, Any]],
    payload_dict: dict[str, Any],
) -> list[dict[str, Any]]:
    """Best-effort: try every notifier, collect each result, never
    raise.  Returns one dict per notifier with the kind + ok + detail
    so ``alerts.notify_results_json`` holds the dispatch ledger."""
    from evalguard_evaluators.notifiers.base import AlertPayload
    from evalguard_evaluators.registry import load_notifier

    payload = AlertPayload(
        schema=payload_dict.get("schema", "evalguard.alert.v1"),
        rule_id=payload_dict["rule_id"],
        project_id=payload_dict["project_id"],
        fired_at=payload_dict["fired_at"],
        window=payload_dict["window"],
        gate=payload_dict["gate"],
        observed_value=payload_dict.get("observed_value"),
        threshold=payload_dict.get("threshold", {}),
        transition=payload_dict["transition"],
        description=payload_dict.get("description"),
        extras={k: v for k, v in payload_dict.items()
                if k not in {
                    "schema", "rule_id", "project_id", "fired_at",
                    "window", "gate", "observed_value", "threshold",
                    "transition", "description",
                }},
    )

    results: list[dict[str, Any]] = []
    for spec in notify_specs:
        kind = spec.get("kind")
        cfg = spec.get("config") or {}
        if not isinstance(kind, str) or not kind:
            results.append({
                "kind": None, "ok": False,
                "detail": "notifier spec missing 'kind'",
            })
            continue
        try:
            notifier = load_notifier(kind, cfg)
            result = await notifier.send(payload)
            results.append({
                "kind": result.kind, "ok": bool(result.ok),
                "detail": result.detail,
            })
        except Exception as e:  # noqa: BLE001
            results.append({
                "kind": kind, "ok": False,
                "detail": f"{type(e).__name__}: {e}",
            })
    return results


# ---------------------------------------------------------------------------
# Per-rule evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertOutcome:
    """What ``evaluate_alert_rule`` returns to the caller."""

    rule_id: str
    decision: StateDecision
    window: WindowResult
    fired: bool
    resolved: bool
    suppressed: bool
    notify_results: list[dict[str, Any]]
    alert_id: int | None


async def evaluate_alert_rule(
    engine: Engine,
    *,
    project_id: str,
    org_id: str,
    rule_id: str,
    rule_cfg: dict[str, Any],
    actor_id: str = "system",
    now: datetime | None = None,
) -> AlertOutcome:
    """Top-level per-rule evaluator.  One DB transaction wraps the
    window read + state UPSERT + alert insert + audit event; notifier
    dispatch happens OUTSIDE the transaction so a slow webhook
    doesn't pin the connection."""
    now = now or datetime.now(timezone.utc)
    window_td = parse_window(rule_cfg["window"])
    gate = rule_cfg.get("gate", "pass_rate")
    threshold = rule_cfg.get("threshold") or {}
    min_samples = int(rule_cfg.get("min_samples", 10))
    suppress_secs = int(rule_cfg.get("suppress_secs", 600))
    notify_specs = rule_cfg.get("notify") or []
    description = rule_cfg.get("description")

    # Phase 1 — read window + state + decide
    with engine.connect() as conn:
        apply_rls_context(conn, org_id=org_id, is_admin=False)
        window = compute_window(
            conn, project_id, gate, window=window_td, now=now,
        )
        prev_state, last_fire_at = _load_state(conn, project_id, rule_id)

    if window.row_count < min_samples:
        # Empty / too-quiet window — skip without changing state
        # so a passing-but-quiet rule doesn't flip on the next
        # busy window.
        return AlertOutcome(
            rule_id=rule_id,
            decision=StateDecision(
                new_state=prev_state, transition="no_change",
                fire=False, resolve=False, suppress=False,
                reason=f"below min_samples ({window.row_count}<{min_samples})",
            ),
            window=window, fired=False, resolved=False, suppressed=False,
            notify_results=[], alert_id=None,
        )

    gate_passes = evaluate_threshold(window.observed_value, threshold)
    decision = state_transition(
        prev_state, gate_passes, last_fire_at, now, suppress_secs,
    )

    if decision.transition == "no_change":
        # Just update last_check_at; no history row, no audit event.
        with engine.begin() as conn:
            apply_rls_context(conn, org_id=org_id, is_admin=False)
            _upsert_state(
                conn, project_id, rule_id,
                state=decision.new_state, now=now, last_fire_at=last_fire_at,
            )
        return AlertOutcome(
            rule_id=rule_id, decision=decision, window=window,
            fired=False, resolved=False, suppressed=False,
            notify_results=[], alert_id=None,
        )

    # Phase 2 — dispatch notifiers (OUTSIDE the DB transaction)
    notify_results: list[dict[str, Any]] = []
    payload_dict = {
        "schema":         "evalguard.alert.v1",
        "rule_id":        rule_id,
        "project_id":     project_id,
        "fired_at":       _now_iso(now),
        "window":         rule_cfg["window"],
        "gate":           gate,
        "observed_value": window.observed_value,
        "threshold":      threshold,
        "transition":     decision.transition,
        "description":    description,
    }
    if decision.fire:
        notify_results = await _dispatch_notifiers(notify_specs, payload_dict)
    elif decision.resolve:
        # Resolution notifier opt-in via the notifier config itself.
        # For v1 we always send a resolve so operators see "the
        # alert closed" — keeps the integration honest.
        notify_results = await _dispatch_notifiers(notify_specs, payload_dict)
    # suppressed: no dispatch.

    # Phase 3 — persist state, alert history, audit event
    alert_id: int | None = None
    new_last_fire = last_fire_at
    if decision.fire:
        new_last_fire = now
    with engine.begin() as conn:
        apply_rls_context(conn, org_id=org_id, is_admin=False)
        _upsert_state(
            conn, project_id, rule_id,
            state=decision.new_state, now=now, last_fire_at=new_last_fire,
        )
        if decision.fire or decision.resolve or decision.suppress:
            alert_id = _insert_alert(
                conn,
                project_id=project_id, rule_id=rule_id, fired_at=now,
                window=window, gate=gate, threshold=threshold,
                transition=decision.transition,
                suppressed=decision.suppress,
                notify_results=notify_results,
            )
        # Audit trail for alerts lives on the ``alerts`` table itself
        # (append-only, with full payload + notify_results captured
        # per row).  We deliberately keep alerts OUT of the per-run
        # ``event_rows`` chain — alerts are cross-run by design and
        # would force every project to carry a synthetic alert-run
        # row, breaking the "one chain per real run" invariant the
        # existing audit verifier relies on.  The ``alert.fired`` /
        # ``alert.resolved`` EVENT_KINDS are reserved for a future
        # pass that may also link individual alerts back into the
        # run chain.

    return AlertOutcome(
        rule_id=rule_id, decision=decision, window=window,
        fired=decision.fire, resolved=decision.resolve,
        suppressed=decision.suppress,
        notify_results=notify_results, alert_id=alert_id,
    )


def _emit_alert_event(
    conn: Connection,
    *,
    kind: str,
    project_id: str,
    rule_id: str,
    payload: dict[str, Any],
    actor_id: str,
) -> None:
    """Alerts are cross-run by design — chain them under a synthetic
    project-level run id so the existing per-run audit chain mechanism
    still applies (it scopes by ``run_id``; we use ``alerts:<project_id>``
    as the chain key)."""
    chain_run_id = f"alerts:{project_id}"
    emit_event(
        conn,
        kind=kind,
        run_id=chain_run_id,
        project_id=project_id,
        trial_id=None,
        row_id=None,
        actor_id=actor_id,
        actor_type="system",
        subject_id=rule_id,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Cron entry
# ---------------------------------------------------------------------------


def _projects_with_alerts_configs(conn: Connection) -> list[dict[str, Any]]:
    """Return every project's latest config row that has an ``alerts:``
    block.  Uses the same ``project_configs`` table the /invoke handler
    reads, so a freshly-pushed alert rule is picked up on the next
    cron tick without restart."""
    rows = conn.execute(
        text("""
            SELECT p.project_id, p.org_id, pc.content
            FROM projects p
            JOIN project_configs pc
              ON pc.id = (
                SELECT id FROM project_configs
                WHERE project_id = p.project_id
                ORDER BY pushed_at DESC, id DESC LIMIT 1
              )
        """),
    ).mappings().fetchall()
    return [dict(r) for r in rows]


async def evaluate_all_alert_rules(
    engine: Engine,
    *,
    now: datetime | None = None,
) -> list[AlertOutcome]:
    """Cron entry — re-evaluate every project's alert rules.

    Called from the Arq worker on a fixed interval.  Errors on one
    rule are isolated to that rule (logged + skipped); the cron tick
    always returns the outcomes list for whatever evaluated cleanly.
    """
    import yaml

    now = now or datetime.now(timezone.utc)
    outcomes: list[AlertOutcome] = []

    # Read project configs in admin context — the cron has no
    # per-org principal.  Per-rule evaluation re-establishes RLS
    # against the project's own org_id before any reads / writes.
    with engine.connect() as conn:
        from evalguard_api.db import apply_admin_rls_context
        apply_admin_rls_context(conn)
        projects = _projects_with_alerts_configs(conn)

    for project in projects:
        try:
            cfg = yaml.safe_load(project["content"]) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning(
                '{"evt":"alert_cron_yaml_parse_failed","project_id":%r,"error":%r}',
                project["project_id"], f"{type(e).__name__}: {e}",
            )
            continue
        rules = cfg.get("alerts") or {}
        if not isinstance(rules, dict):
            continue
        for rule_id, rule_cfg in rules.items():
            if not isinstance(rule_cfg, dict):
                continue
            try:
                outcome = await evaluate_alert_rule(
                    engine,
                    project_id=project["project_id"],
                    org_id=project["org_id"],
                    rule_id=str(rule_id),
                    rule_cfg=rule_cfg,
                    now=now,
                )
                outcomes.append(outcome)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    '{"evt":"alert_eval_failed","project_id":%r,'
                    '"rule_id":%r,"error":%r}',
                    project["project_id"], rule_id,
                    f"{type(e).__name__}: {e}",
                )
    return outcomes
