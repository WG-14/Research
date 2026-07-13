"""Only production composition root importing concrete built-in plugins."""
from __future__ import annotations
from functools import lru_cache

from market_research.research.strategies.buy_and_hold_baseline import build_buy_and_hold_baseline_plugin
from market_research.research.strategies.noop_baseline import build_noop_baseline_plugin
from market_research.research.strategies.sma_with_filter import build_sma_with_filter_plugin
from market_research.research.strategies.threshold_research_only import build_threshold_research_only_plugin
from market_research.research.strategy_registry import StrategyRegistry


@lru_cache(maxsize=1)
def builtin_strategy_registry() -> StrategyRegistry:
    return StrategyRegistry.build((build_sma_with_filter_plugin(), build_buy_and_hold_baseline_plugin(),
                                   build_noop_baseline_plugin(), build_threshold_research_only_plugin()))
