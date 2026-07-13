"""Compatibility facade over the immutable built-in strategy registry."""
from __future__ import annotations

from .builtin_registry import builtin_strategy_registry
from .strategy_contract import ResearchStrategyDataRequirements, ResearchStrategyPlugin
from .strategy_registry import StrategyRegistryError

ResearchStrategyCatalogError = StrategyRegistryError


def resolve_research_strategy(name: str) -> ResearchStrategyPlugin:
    return builtin_strategy_registry().resolve(name)


def list_research_strategies() -> tuple[ResearchStrategyPlugin, ...]:
    registry = builtin_strategy_registry()
    return tuple(registry.plugins[name] for name in sorted(registry.plugins))


def research_strategy_data_requirements(strategy_name: str, *, strategy_spec: object | None = None) -> ResearchStrategyDataRequirements:
    return resolve_research_strategy(strategy_name).data_requirements(strategy_spec)


def register_research_strategy(plugin: ResearchStrategyPlugin) -> None:
    raise ResearchStrategyCatalogError("immutable_strategy_registry")
