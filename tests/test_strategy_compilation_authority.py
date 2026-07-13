from market_research.research.builtin_registry import builtin_strategy_registry
from market_research.research.strategy_compiler import StrategyCompiler
import market_research.research.strategy_compiler as compiler_module


def test_parameter_source_map_covers_every_materialized_parameter():
    contract = StrategyCompiler(builtin_strategy_registry()).compile(strategy_name="sma_with_filter",
        raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3}, fee_rate=.001, slippage_bps=10)
    assert set(contract.materialized_parameters) == set(contract.parameter_source_map)
    assert contract.compiled_contract_hash.startswith("sha256:")


def test_same_candidate_scenario_compiles_identically():
    compiler = StrategyCompiler(builtin_strategy_registry())
    values = dict(strategy_name="noop_baseline", raw_parameters={}, fee_rate=.001, slippage_bps=10)
    assert compiler.compile(**values).compiled_contract_hash == compiler.compile(**values).compiled_contract_hash


def test_spec_materialization_occurs_once_per_candidate_scenario(monkeypatch):
    calls = 0
    original = compiler_module.materialize_parameters_from_spec

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler_module, "materialize_parameters_from_spec", counted)
    StrategyCompiler(builtin_strategy_registry()).compile(strategy_name="sma_with_filter",
        raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3}, fee_rate=.001, slippage_bps=10)
    assert calls == 1


def test_plugin_extension_receives_materialized_parameters():
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    observed = {}

    def extension(*, materialized_parameters, **kwargs):
        observed.update(materialized_parameters)
        return materialized_parameters

    from dataclasses import replace
    from market_research.research.strategy_registry import StrategyRegistry
    changed = replace(plugin, parameter_materializer=extension)
    StrategyCompiler(StrategyRegistry.build((changed,))).compile(strategy_name=changed.name,
        raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3}, fee_rate=.001, slippage_bps=10)
    assert observed["LIVE_FEE_RATE_ESTIMATE"] == .001
    assert observed["STRATEGY_ENTRY_SLIPPAGE_BPS"] == 10
