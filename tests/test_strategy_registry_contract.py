import pytest
from market_research.research.builtin_registry import builtin_strategy_registry
from market_research.research.strategy_registry import StrategyRegistry, StrategyRegistryError


def test_registry_is_immutable_and_unknown_fails_closed():
    registry = builtin_strategy_registry()
    with pytest.raises(TypeError):
        registry.plugins["x"] = registry.resolve("noop_baseline")
    with pytest.raises(StrategyRegistryError, match="unsupported"):
        registry.resolve("__test_alias")


def test_duplicate_strategy_name_is_rejected():
    plugin = builtin_strategy_registry().resolve("noop_baseline")
    with pytest.raises(StrategyRegistryError, match="duplicate"):
        StrategyRegistry.build((plugin, plugin))


def test_registry_hash_changes_when_plugin_contract_changes():
    from dataclasses import replace
    plugin = builtin_strategy_registry().resolve("noop_baseline")
    changed = replace(plugin, version=plugin.version + ".changed")
    assert StrategyRegistry.build((plugin,)).content_hash != StrategyRegistry.build((changed,)).content_hash
