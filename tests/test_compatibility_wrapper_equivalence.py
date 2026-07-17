from dataclasses import replace

from market_research.research.backtest_engine import (
    run_buy_and_hold_baseline_backtest,
    run_registered_strategy_backtest,
)
from market_research.research_composition import builtin_strategy_registry
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_common_simulation_engine import _dataset
from market_research.research.strategy_registry import StrategyRegistry
from market_research.strategy_sdk.runtime import make_event_builder_runtime_factory


def test_compatibility_wrapper_stream_hashes_equal_direct_engine():
    values = dict(
        dataset=_dataset(),
        parameter_values={"BUY_HOLD_BUY_INDEX": 1},
        fee_rate=0.001,
        slippage_bps=10,
    )
    wrapped = run_buy_and_hold_baseline_backtest(
        **values, strategy_registry=builtin_strategy_registry()
    )
    direct = run_common_simulation_backtest(
        plugin=resolve_research_strategy("buy_and_hold_baseline"), **values
    )
    for key in (
        "execution_request_stream_hash",
        "execution_fill_stream_hash",
        "ledger_stream_hash",
    ):
        assert (
            wrapped.execution_event_summary[key] == direct.execution_event_summary[key]
        )
    assert wrapped.metrics_hash == direct.metrics_hash


def test_new_strategy_uses_public_generic_runner_without_named_wrapper():
    base = resolve_research_strategy("noop_baseline")

    def event_builder(**_values):
        return ()

    plugin = replace(
        base,
        name="fixture_strategy",
        version="fixture_strategy.v1",
        spec=replace(
            base.spec,
            strategy_name="fixture_strategy",
            strategy_version="fixture_strategy.v1",
        ),
        event_builder=event_builder,
        runtime_factory=make_event_builder_runtime_factory(
            event_builder, current_candle_only=True
        ),
    )
    registry = StrategyRegistry.build((plugin,))

    run = run_registered_strategy_backtest(
        "fixture_strategy",
        dataset=_dataset(),
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
        strategy_registry=registry,
    )

    assert run.compiled_strategy_contract.strategy_name == "fixture_strategy"
