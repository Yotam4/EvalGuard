"""Audit-hook hand-off between evaluators and whatever orchestrates them.

Evaluators in this package (notably the ``pointwise`` judge) need to
emit ``provider.called`` audit events for their own LLM calls, but
they can't depend on ``evalguard_cli`` — the CLI is allowed to import
evaluators, not the other way around. This module owns the hand-off:

- The orchestrator (CLI executor) constructs an ``AuditHook``-shaped
  object and binds it via ``set_audit_hook`` for the duration of a
  single row's evaluation.
- The judge reads the bound hook via ``current_audit_hook``.
- The hook is stored in a ``ContextVar`` so concurrent rows running
  on the *same* judge instance under ``asyncio.gather`` don't
  trample each other.

The shape of ``AuditHook`` itself (``emit_provider_call(...)`` etc.)
is duck-typed — evaluators only check ``hook is not None`` and call
the methods they need. Keeping it untyped here means the CLI can
evolve the hook signature without rippling through evaluator code.
"""

from __future__ import annotations

import contextvars
from typing import Any


_audit_hook_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "evalguard_audit_hook", default=None,
)


def current_audit_hook() -> Any:
    """Return the audit hook bound to the running evaluator, if any."""
    return _audit_hook_var.get()


def set_audit_hook(hook: Any) -> contextvars.Token:
    """Bind an audit hook to the current task; returns a reset token."""
    return _audit_hook_var.set(hook)


def reset_audit_hook(token: contextvars.Token) -> None:
    _audit_hook_var.reset(token)
