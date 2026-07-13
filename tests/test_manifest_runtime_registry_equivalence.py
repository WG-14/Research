from market_research.research.builtin_registry import builtin_strategy_registry
from market_research.research.experiment_manifest import load_manifest
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.strategy_registry import StrategyRegistry
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
