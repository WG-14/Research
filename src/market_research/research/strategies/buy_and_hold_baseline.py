"""Compatibility lookup requiring an explicitly selected registry."""

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_registry import StrategyRegistry


def build_buy_and_hold_baseline_plugin(
    *, registry: StrategyRegistry
) -> ResearchStrategyPlugin:
    return registry.resolve("buy_and_hold_baseline")


__all__ = ["build_buy_and_hold_baseline_plugin"]
