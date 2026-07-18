"""Compatibility lookup requiring an explicitly selected registry."""

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_registry import StrategyRegistry


def build_noop_baseline_plugin(*, registry: StrategyRegistry) -> ResearchStrategyPlugin:
    return registry.resolve("noop_baseline")


__all__ = ["build_noop_baseline_plugin"]
