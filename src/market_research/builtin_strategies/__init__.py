"""Deterministic discovery for built-in, research-only strategy plugins."""
from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules
from typing import Callable


class BuiltinStrategyDiscoveryError(RuntimeError):
    pass


def discover_builtin_strategy_factories() -> tuple[Callable[[], object], ...]:
    """Discover explicitly marked plugin modules in stable module-name order."""
    factories: list[Callable[[], object]] = []
    for module_info in sorted(iter_modules(__path__, prefix=f"{__name__}."), key=lambda item: item.name):
        module = import_module(module_info.name)
        factory = getattr(module, "STRATEGY_PLUGIN_FACTORY", None)
        if factory is None:
            continue
        if not callable(factory):
            raise BuiltinStrategyDiscoveryError(
                f"builtin_strategy_factory_not_callable:{module_info.name}"
            )
        factories.append(factory)
    if not factories:
        raise BuiltinStrategyDiscoveryError("builtin_strategy_factories_missing")
    return tuple(factories)


__all__ = ["BuiltinStrategyDiscoveryError", "discover_builtin_strategy_factories"]
