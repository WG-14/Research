from market_research.research_composition import builtin_strategy_registry
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_compiler import StrategyCompiler
from tests.test_common_simulation_engine import _dataset


def _compiled(fee: float, slippage: float):
    registry = builtin_strategy_registry()
    return StrategyCompiler(registry).compile(
        strategy_name="sma_with_filter",
        raw_parameters={"SMA_SHORT": 1, "SMA_LONG": 2},
        fee_rate=fee,
        slippage_bps=slippage,
    )


def test_train_validation_holdout_share_candidate_scenario_contract():
    registry = builtin_strategy_registry()
    plugin = registry.resolve("sma_with_filter")
    compiled = _compiled(0.001, 10)
    runs = [
        run_common_simulation_backtest(
            plugin=plugin,
            dataset=_dataset(),
            parameter_values={"SMA_SHORT": 1, "SMA_LONG": 2},
            fee_rate=0.001,
            slippage_bps=10,
            compiled_contract=compiled,
            registry=registry,
        )
        for _split in ("train", "validation", "final_holdout")
    ]
    assert all(run.compiled_strategy_contract is compiled for run in runs)
    assert {run.compiled_strategy_contract_hash for run in runs} == {
        compiled.compiled_contract_hash
    }


def test_each_scenario_binds_its_own_compiled_contract():
    base = _compiled(0.001, 10)
    stress = _compiled(0.002, 25)
    assert base.compiled_contract_hash != stress.compiled_contract_hash
    assert base.materialized_parameters_hash != stress.materialized_parameters_hash
