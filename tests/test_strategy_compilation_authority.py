from market_research.research.builtin_registry import builtin_strategy_registry
from market_research.research.strategy_compiler import StrategyCompiler


def test_parameter_source_map_covers_every_materialized_parameter():
    contract = StrategyCompiler(builtin_strategy_registry()).compile(strategy_name="sma_with_filter",
        raw_parameters={"SMA_SHORT": 2, "SMA_LONG": 3}, fee_rate=.001, slippage_bps=10)
    assert set(contract.materialized_parameters) == set(contract.parameter_source_map)
    assert contract.compiled_contract_hash.startswith("sha256:")


def test_same_candidate_scenario_compiles_identically():
    compiler = StrategyCompiler(builtin_strategy_registry())
    values = dict(strategy_name="noop_baseline", raw_parameters={}, fee_rate=.001, slippage_bps=10)
    assert compiler.compile(**values).compiled_contract_hash == compiler.compile(**values).compiled_contract_hash
