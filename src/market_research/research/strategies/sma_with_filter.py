"""Compatibility lookup requiring an explicitly selected registry."""

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_registry import StrategyRegistry


def build_sma_with_filter_plugin(
    *, registry: StrategyRegistry
) -> ResearchStrategyPlugin:
    return registry.resolve("sma_with_filter")


__all__ = ["build_sma_with_filter_plugin"]
