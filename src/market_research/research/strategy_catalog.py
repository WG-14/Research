"""Dependency-free compatibility helpers for an explicit strategy registry."""

from __future__ import annotations

from .strategy_contract import ResearchStrategyDataRequirements, ResearchStrategyPlugin
from .strategy_registry import StrategyRegistry, StrategyRegistryError

ResearchStrategyCatalogError = StrategyRegistryError


def resolve_research_strategy(
    name: str, *, registry: StrategyRegistry
) -> ResearchStrategyPlugin:
    return registry.resolve(name)


def list_research_strategies(
    *, registry: StrategyRegistry
) -> tuple[ResearchStrategyPlugin, ...]:
    return tuple(registry.plugins[name] for name in sorted(registry.plugins))


def research_strategy_data_requirements(
    strategy_name: str,
    *,
    registry: StrategyRegistry,
    strategy_spec: object | None = None,
) -> ResearchStrategyDataRequirements:
    return resolve_research_strategy(
        strategy_name, registry=registry
    ).data_requirements(strategy_spec)


def register_research_strategy(plugin: ResearchStrategyPlugin) -> None:
    raise ResearchStrategyCatalogError("immutable_strategy_registry")
