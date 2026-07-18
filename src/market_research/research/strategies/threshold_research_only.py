"""Compatibility lookup requiring an explicitly selected registry."""

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_registry import StrategyRegistry


def build_threshold_research_only_plugin(
    *, registry: StrategyRegistry
) -> ResearchStrategyPlugin:
    return registry.resolve("threshold_research_only")


__all__ = ["build_threshold_research_only_plugin"]
