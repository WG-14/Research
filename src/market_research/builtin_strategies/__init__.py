"""Deterministic discovery for built-in, research-only strategy plugins."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pkgutil import iter_modules
from typing import Callable


class BuiltinStrategyDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BuiltinStrategyLoadFailure:
    module_name: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class BuiltinStrategyDiscoveryReport:
    factories: tuple[Callable[[], object], ...]
    failures: tuple[BuiltinStrategyLoadFailure, ...]
    factory_module_names: tuple[str, ...] = ()


def discover_builtin_strategy_modules() -> BuiltinStrategyDiscoveryReport:
    """Import each marked module independently and retain stable failures."""
    factories: list[Callable[[], object]] = []
    factory_module_names: list[str] = []
    failures: list[BuiltinStrategyLoadFailure] = []
    for module_info in sorted(
        iter_modules(__path__, prefix=f"{__name__}."), key=lambda item: item.name
    ):
        try:
            module = import_module(module_info.name)
        except Exception as exc:
            failures.append(
                BuiltinStrategyLoadFailure(
                    module_info.name,
                    f"module_import_failed:{type(exc).__name__}",
                )
            )
            continue
        factory = getattr(module, "STRATEGY_PLUGIN_FACTORY", None)
        if factory is None:
            continue
        if not callable(factory):
            failures.append(
                BuiltinStrategyLoadFailure(
                    module_info.name,
                    "strategy_plugin_factory_not_callable",
                )
            )
            continue
        factories.append(factory)
        factory_module_names.append(module_info.name)
    return BuiltinStrategyDiscoveryReport(
        tuple(factories),
        tuple(failures),
        tuple(factory_module_names),
    )


def discover_builtin_strategy_factories() -> tuple[Callable[[], object], ...]:
    """Compatibility projection of all independently available factories."""
    report = discover_builtin_strategy_modules()
    if not report.factories:
        raise BuiltinStrategyDiscoveryError("builtin_strategy_factories_missing")
    return report.factories


__all__ = [
    "BuiltinStrategyDiscoveryError",
    "BuiltinStrategyDiscoveryReport",
    "BuiltinStrategyLoadFailure",
    "discover_builtin_strategy_factories",
    "discover_builtin_strategy_modules",
]
