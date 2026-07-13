import json

from market_research.research.builtin_registry import builtin_strategy_registry
from market_research.research.experiment_manifest import load_manifest
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.strategy_registry import StrategyRegistry
from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategyParameterSchema, StrategySpec
from tests.research_sma_success_fixture import create_success_fixture
from tests.test_common_simulation_engine import _dataset


def test_custom_registry_flows_through_manifest_and_simulation(tmp_path):
    _, manifest_path = create_success_fixture(tmp_path)
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    registry = StrategyRegistry.build((plugin,))
    manifest = load_manifest(manifest_path, registry=registry)
    assert manifest.strategy_name == plugin.name
    compiled = StrategyCompiler(registry).compile(strategy_name=plugin.name,
        raw_parameters={"SMA_SHORT": 1, "SMA_LONG": 2}, fee_rate=0, slippage_bps=0)
    run = run_common_simulation_backtest(plugin=plugin, registry=registry,
        compiled_contract=compiled, dataset=_dataset(),
        parameter_values={"SMA_SHORT": 1, "SMA_LONG": 2}, fee_rate=0, slippage_bps=0)
    assert run.strategy_registry_hash == registry.content_hash == compiled.strategy_registry_hash


def test_unknown_strategy_fails_closed_in_manifest(tmp_path):
    _, manifest_path = create_success_fixture(tmp_path)
    registry = StrategyRegistry.build((builtin_strategy_registry().resolve("noop_baseline"),))
    try:
        load_manifest(manifest_path, registry=registry)
    except ValueError as exc:
        assert "unsupported_research_strategy" in str(exc)
    else:
        raise AssertionError("unknown manifest strategy was accepted")


def _custom_plugin() -> ResearchStrategyPlugin:
    spec = StrategySpec("fixture_custom_alpha", "fixture.v1", ("WINDOW",), ("WINDOW",),
        ("WINDOW",), (), (), {"WINDOW": 7}, "fixture_decision.v1", ("candles",), (),
        {"schema_version": 1, "rules": ()}, (StrategyParameterSchema("WINDOW", "int", min_value=1),))
    return ResearchStrategyPlugin(name=spec.strategy_name, version=spec.strategy_version, spec=spec,
        required_data=spec.required_data, optional_data=(), event_builder=lambda **_values: (),
        decision_contract_version=spec.decision_contract_version, diagnostics_namespace="fixture")


def test_true_custom_strategy_name_parses_and_compiles_without_global_catalog(tmp_path, monkeypatch):
    _, path = create_success_fixture(tmp_path)
    payload = json.loads(path.read_text())
    payload["strategy_name"] = "fixture_custom_alpha"
    payload["parameter_space"] = {"WINDOW": [7]}
    path.write_text(json.dumps(payload))
    registry = StrategyRegistry.build((_custom_plugin(),))
    manifest = load_manifest(path, registry=registry)
    monkeypatch.setattr("market_research.research.strategy_spec.strategy_spec_for_name",
                        lambda _name: (_ for _ in ()).throw(AssertionError("built-in catalog called")))
    compiled = StrategyCompiler(registry).compile(strategy_name=manifest.strategy_name,
        raw_parameters={}, fee_rate=0, slippage_bps=0)
    assert compiled.materialized_parameters["WINDOW"] == 7
    assert compiled.parameter_source_map["WINDOW"] == "strategy_spec_default"
    assert compiled.strategy_registry_hash == registry.content_hash
