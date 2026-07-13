"""Only production composition root importing concrete built-in plugins."""
from __future__ import annotations
from functools import lru_cache

from market_research.builtin_strategies.buy_and_hold_baseline import build_buy_and_hold_baseline_plugin
from market_research.builtin_strategies.noop_baseline import build_noop_baseline_plugin
from market_research.builtin_strategies.sma_with_filter import build_sma_with_filter_plugin
from market_research.builtin_strategies.threshold_research_only import build_threshold_research_only_plugin
from market_research.research.strategy_registry import StrategyRegistry
from market_research.research.strategy_compiler import StrategyCompiler


@lru_cache(maxsize=1)
def builtin_strategy_registry() -> StrategyRegistry:
    return StrategyRegistry.build((build_sma_with_filter_plugin(), build_buy_and_hold_baseline_plugin(),
                                   build_noop_baseline_plugin(), build_threshold_research_only_plugin()))


def compile_builtin_strategy(**values):
    """Composition-owned convenience for production built-in selection."""
    registry = builtin_strategy_registry()
    return StrategyCompiler(registry).compile(**values)
