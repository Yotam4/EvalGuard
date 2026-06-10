"""In-process notifier used by tests.

Records every dispatched payload to a class-level list so test code
can assert "alert N fired with shape X" without an outbound HTTP
call.  The list is class-scoped (not instance-scoped) because the
registry instantiates a fresh notifier per dispatch — tests reset
the list between cases via ``MockNotifier.reset()``.
"""

from __future__ import annotations

from typing import Any

from evalguard_evaluators.notifiers.base import (
    AlertPayload, NotifyResult,
)


class MockNotifier:
    kind = "mock"

    # Class-level so tests can read what was sent without holding
    # the notifier instance.  Reset between tests via ``reset()``.
    sent: list[AlertPayload] = []

    def __init__(self) -> None:
        self._label: str = "mock"

    def configure(self, cfg: dict[str, Any]) -> None:
        self._label = cfg.get("label", "mock")

    async def send(self, payload: AlertPayload) -> NotifyResult:
        MockNotifier.sent.append(payload)
        return NotifyResult(kind=self.kind, ok=True,
                            detail=f"label={self._label}")

    @classmethod
    def reset(cls) -> None:
        cls.sent.clear()
