"""Only production composition root importing concrete built-in plugins."""
from __future__ import annotations
from functools import lru_cache

from market_research.builtin_strategies.buy_and_hold_baseline import build_buy_and_hold_baseline_plugin
from market_research.builtin_strategies.noop_baseline import build_noop_baseline_plugin
from market_research.builtin_strategies.sma_with_filter import build_sma_with_filter_plugin
from market_research.builtin_strategies.threshold_research_only import build_threshold_research_only_plugin
from market_research.research.strategy_registry import StrategyRegistry
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.experiment_manifest import (
    ExperimentManifest,
    load_manifest_with_registry,
    parse_manifest_with_registry,
)


@lru_cache(maxsize=1)
def builtin_strategy_registry() -> StrategyRegistry:
    return StrategyRegistry.build((build_sma_with_filter_plugin(), build_buy_and_hold_baseline_plugin(),
                                   build_noop_baseline_plugin(), build_threshold_research_only_plugin()))


def compile_builtin_strategy(**values):
    """Composition-owned convenience for production built-in selection."""
    registry = builtin_strategy_registry()
    return StrategyCompiler(registry).compile(**values)


def load_builtin_manifest(path: str) -> ExperimentManifest:
    """Composition-owned convenience loader for the built-in registry."""
    return load_manifest_with_registry(path, registry=builtin_strategy_registry())


def parse_builtin_manifest(payload: dict[str, object]) -> ExperimentManifest:
    """Composition-owned convenience parser for the built-in registry."""
    return parse_manifest_with_registry(payload, registry=builtin_strategy_registry())


def resolve_builtin_strategy(name: str):
    return builtin_strategy_registry().resolve(name)


def list_builtin_strategies() -> tuple[object, ...]:
    registry = builtin_strategy_registry()
    return tuple(registry.plugins[name] for name in sorted(registry.plugins))
