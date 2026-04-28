"""Lookup evaluators and providers via Python entry points."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from evalguard_evaluators.base import Evaluator, Provider

_EVALUATOR_GROUP = "evalguard.evaluators"
_PROVIDER_GROUP = "evalguard.providers"


def iter_evaluators() -> dict[str, type[Evaluator]]:
    out: dict[str, type[Evaluator]] = {}
    for ep in entry_points(group=_EVALUATOR_GROUP):
        out[ep.name] = ep.load()
    return out


def iter_providers() -> dict[str, type[Provider]]:
    out: dict[str, type[Provider]] = {}
    for ep in entry_points(group=_PROVIDER_GROUP):
        out[ep.name] = ep.load()
    return out


def load_evaluator(name: str, cfg: dict[str, Any]) -> Evaluator:
    cls = iter_evaluators().get(name)
    if cls is None:
        raise KeyError(f"unknown evaluator '{name}' (available: {sorted(iter_evaluators())})")
    inst = cls()
    inst.configure(cfg)
    return inst


def load_provider(name: str, cfg: dict[str, Any]) -> Provider:
    cls = iter_providers().get(name)
    if cls is None:
        raise KeyError(f"unknown provider '{name}' (available: {sorted(iter_providers())})")
    inst = cls()
    inst.configure(cfg)
    return inst
