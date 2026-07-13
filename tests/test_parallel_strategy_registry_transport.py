from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor

import pytest

from market_research.research_composition import builtin_strategy_registry
from market_research.research.strategy_registry import (StrategyRegistry, StrategyRegistryError,
    reconstruct_strategy_registry)
from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategyRuleDeclaration, StrategyRuleSpec, StrategySpec


def _reconstruct_hash(descriptor):
    return reconstruct_strategy_registry(descriptor).content_hash


def _custom_events(**_values):
    return ()


def build_importable_custom_plugin():
    rules = StrategyRuleSpec(1,
        entry=StrategyRuleDeclaration("fixture_entry", "Fixture entry rule.", "never"),
        take_profit=StrategyRuleDeclaration("take_profit", "Disabled.", "never"),
        edge_invalidation=StrategyRuleDeclaration("edge_invalidation", "Disabled.", "never"),
        time_exit=StrategyRuleDeclaration("time_exit", "Disabled.", "never"),
        stop_loss=StrategyRuleDeclaration("stop_loss", "Disabled.", "never"),
        position_sizing=StrategyRuleDeclaration("no_position", "No allocation.", "always"))
    spec = StrategySpec("transport_custom", "v1", (), (), (), (), (), {}, "v1", ("candles",), (),
                        {"schema_version": 1, "rules": ()}, rule_spec=rules)
    return ResearchStrategyPlugin(name=spec.strategy_name, version=spec.strategy_version, spec=spec,
        required_data=("candles",), optional_data=(), event_builder=_custom_events,
        decision_contract_version="v1", diagnostics_namespace="custom", reconstruction_module=__name__,
        reconstruction_qualname="build_importable_custom_plugin")


def test_registry_descriptor_round_trip_and_deterministic_order():
    registry = builtin_strategy_registry()
    descriptor = registry.descriptor()
    assert [item["strategy_name"] for item in descriptor["plugins"]] == sorted(registry.plugins)
    assert reconstruct_strategy_registry(descriptor).content_hash == registry.content_hash
    assert descriptor == registry.descriptor()


def test_worker_plugin_and_registry_hash_mismatch_is_rejected():
    descriptor = builtin_strategy_registry().descriptor()
    descriptor["plugins"][0]["plugin_contract_hash"] = "sha256:" + "0" * 64
    material = {key: descriptor[key] for key in ("schema_version", "registry_content_hash", "plugins")}
    from market_research.research.hashing import sha256_prefixed
    descriptor["descriptor_hash"] = sha256_prefixed(material)
    with pytest.raises(StrategyRegistryError, match="plugin_contract_hash_mismatch"):
        reconstruct_strategy_registry(descriptor)


def test_parallel_custom_importable_plugin_runs_and_unreconstructable_fails_closed():
    registry = StrategyRegistry.build((build_importable_custom_plugin(),))
    assert reconstruct_strategy_registry(registry.descriptor()).resolve("transport_custom").name == "transport_custom"
    with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as pool:
        assert pool.submit(_reconstruct_hash, registry.descriptor()).result(timeout=30) == registry.content_hash
    plugin = build_importable_custom_plugin()
    object.__setattr__(plugin, "reconstruction_module", None)
    with pytest.raises(StrategyRegistryError, match="not_reconstructable"):
        StrategyRegistry.build((plugin,)).descriptor()


def _assert_parallel_method(method: str) -> None:
    registry = builtin_strategy_registry()
    with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context(method)) as pool:
        assert pool.submit(_reconstruct_hash, registry.descriptor()).result(timeout=30) == registry.content_hash


def test_parallel_spawn_reconstructs_same_registry_hash():
    _assert_parallel_method("spawn")


def test_parallel_forkserver_reconstructs_same_registry_hash():
    _assert_parallel_method("forkserver")


def test_parallel_fork_reconstructs_same_registry_hash_when_supported():
    _assert_parallel_method("fork")
