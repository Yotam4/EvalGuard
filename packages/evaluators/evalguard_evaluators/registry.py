"""Lookup evaluators and providers via Python entry points."""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import entry_points
from typing import Any

from evalguard_evaluators.base import Evaluator, Provider

_EVALUATOR_GROUP = "evalguard.evaluators"
_PROVIDER_GROUP = "evalguard.providers"
_NOTIFIER_GROUP = "evalguard.notifiers"


# Entry-point discovery is the slowest single op in the hot path —
# ``importlib.metadata.entry_points`` walks every installed package's
# metadata. With per-row provider/params overrides (Tier B), every row
# of the dataset triggers a fresh ``load_provider`` call; without
# memoization that scan dominates runtime for cache-cold runs.
#
# Caching is safe in this process: entry points come from installed
# distributions, which don't change at runtime. Tests that introduce
# new evaluators do so via ``sys.modules`` injection of custom_check
# modules, not via entry points, so this cache doesn't shadow them.


@lru_cache(maxsize=1)
def _evaluator_classes() -> dict[str, type[Evaluator]]:
    return {ep.name: ep.load() for ep in entry_points(group=_EVALUATOR_GROUP)}


@lru_cache(maxsize=1)
def _provider_classes() -> dict[str, type[Provider]]:
    return {ep.name: ep.load() for ep in entry_points(group=_PROVIDER_GROUP)}


@lru_cache(maxsize=1)
def _notifier_classes() -> dict[str, type]:
    """Notifier plugins from the ``evalguard.notifiers`` entry-point
    group.  Type kept loose — ``Notifier`` is a Protocol defined in
    the evaluators subpackage and circular-imports if we pin it
    here."""
    return {ep.name: ep.load() for ep in entry_points(group=_NOTIFIER_GROUP)}


def iter_evaluators() -> dict[str, type[Evaluator]]:
    """Return the registered evaluator classes (cached after first call)."""
    return dict(_evaluator_classes())


def iter_providers() -> dict[str, type[Provider]]:
    """Return the registered provider classes (cached after first call)."""
    return dict(_provider_classes())


def load_evaluator(name: str, cfg: dict[str, Any]) -> Evaluator:
    cls = _evaluator_classes().get(name)
    if cls is None:
        raise KeyError(
            f"unknown evaluator '{name}' (available: {sorted(_evaluator_classes())})"
        )
    inst = cls()
    inst.configure(cfg)
    return inst


def load_provider(name: str, cfg: dict[str, Any]) -> Provider:
    cls = _provider_classes().get(name)
    if cls is None:
        raise KeyError(
            f"unknown provider '{name}' (available: {sorted(_provider_classes())})"
        )
    inst = cls()
    inst.configure(cfg)
    return inst


def load_notifier(name: str, cfg: dict[str, Any]) -> Any:
    cls = _notifier_classes().get(name)
    if cls is None:
        raise KeyError(
            f"unknown notifier '{name}' (available: {sorted(_notifier_classes())})"
        )
    inst = cls()
    inst.configure(cfg)
    return inst


def iter_notifiers() -> dict[str, type]:
    return dict(_notifier_classes())


def reset_registry_cache() -> None:
    """Drop the entry-point caches. Used by tests that install plugins
    via temporary entry points; production code never needs this."""
    _evaluator_classes.cache_clear()
    _provider_classes.cache_clear()
    _notifier_classes.cache_clear()
