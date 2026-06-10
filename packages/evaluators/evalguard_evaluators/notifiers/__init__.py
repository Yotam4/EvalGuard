"""Notifier plugins for the rolling-window alerting engine.

Notifiers receive an ``AlertPayload`` describing a fired alert
(rule id, observed value, transition kind, ...) and dispatch it to
an external sink — Slack, PagerDuty, a generic webhook, an inbox.
The shipped v1 set is intentionally small:

- ``webhook`` — POST JSON to a configured URL with an optional
  HMAC signature header.  Covers most integrations (Zapier, n8n,
  custom routers) without locking the schema into one provider.
- ``mock`` — records every dispatch to an in-process list; tests
  use this to assert "an alert fired" without an outbound HTTP
  call.

Real-world Slack / PagerDuty / Opsgenie integrations ship as out-
of-repo plugins that register under the same
``evalguard.notifiers`` entry-point group.
"""

from evalguard_evaluators.notifiers.base import (
    AlertPayload, Notifier, NotifyResult,
)

__all__ = ["AlertPayload", "Notifier", "NotifyResult"]
