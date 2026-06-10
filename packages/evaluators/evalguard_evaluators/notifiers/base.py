"""Shared types every notifier implements.

``Notifier`` mirrors the ``Evaluator`` protocol shape (configure +
async dispatch) so the same registry pattern works for both.
``AlertPayload`` is the canonical wire shape that ships to a
notifier — keeping it stable means a Slack plugin written today
keeps working when the alert engine adds new transition kinds
tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AlertPayload:
    """One fired-alert event, handed to notifiers.

    Notifiers serialise this to JSON / form-encode it / whatever
    their downstream needs.  Kept as a typed dataclass so the
    contract is grep-able and a wire-shape change forces an
    explicit update on every notifier.
    """

    schema: str
    rule_id: str
    project_id: str
    fired_at: str
    window: str
    gate: str
    observed_value: float | None
    threshold: dict[str, Any]
    transition: str            # "pass_to_fail" | "fail_to_pass"
    description: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema":         self.schema,
            "rule_id":        self.rule_id,
            "project_id":     self.project_id,
            "fired_at":       self.fired_at,
            "window":         self.window,
            "gate":           self.gate,
            "observed_value": self.observed_value,
            "threshold":      self.threshold,
            "transition":     self.transition,
            "description":    self.description,
            **self.extras,
        }


@dataclass(frozen=True)
class NotifyResult:
    """What a notifier reports back.  Lands in
    ``alerts.notify_results_json`` so an operator can introspect
    delivery failures without grepping logs."""

    kind:    str       # the notifier kind that ran ("webhook", "mock", ...)
    ok:      bool
    detail:  str | None = None


@runtime_checkable
class Notifier(Protocol):
    kind: str

    def configure(self, cfg: dict[str, Any]) -> None: ...

    async def send(self, payload: AlertPayload) -> NotifyResult: ...
