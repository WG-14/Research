from dataclasses import replace

import pytest
from market_research.research_composition import builtin_strategy_registry
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
    plugin = builtin_strategy_registry().resolve("noop_baseline")
    changed = replace(plugin, version=plugin.version + ".changed")
    assert StrategyRegistry.build((plugin,)).content_hash != StrategyRegistry.build((changed,)).content_hash


def test_selected_execution_hash_ignores_unrelated_catalog_additions():
    registry = builtin_strategy_registry()
    selected = registry.resolve("noop_baseline")
    unrelated = replace(
        selected,
        name="unrelated_fixture",
        version="unrelated_fixture.v1",
    )
    expanded = StrategyRegistry.build((*registry.plugins.values(), unrelated))

    assert expanded.content_hash != registry.content_hash
    assert expanded.execution_scope_hash(selected.name) == registry.execution_scope_hash(selected.name)


def test_nested_spec_mutation_is_rejected():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    with pytest.raises(TypeError):
        plugin.spec.default_parameters["SMA_FILTER_GAP_MIN_RATIO"] = 99
    with pytest.raises(TypeError):
        plugin.spec.exit_policy_schema["rules"] = ("changed",)


def test_registry_detects_stale_plugin_contract():
    plugin = builtin_strategy_registry().resolve("noop_baseline")
    registry = StrategyRegistry.build((plugin,))
    object.__setattr__(plugin, "version", plugin.version + ".mutated")
    try:
        with pytest.raises(StrategyRegistryError, match="stale_research_strategy_contract"):
            registry.resolve(plugin.name)
    finally:
        object.__setattr__(plugin, "version", plugin.version.removesuffix(".mutated"))
