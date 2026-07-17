from dataclasses import replace

from market_research.research.execution_model import StressExecutionModel
from market_research.research.experiment_manifest import (
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.strategy_registry import StrategyRegistry
from tests.test_research_threshold_behavior_equivalence import _data


def _observing_plugin(observed):
    base = resolve_research_strategy("threshold_research_only")
    base_factory = base.runtime_factory

    def factory(**kwargs):
        delegate = base_factory(**kwargs)

        class Runtime:
            def initialize(self, context):
                return delegate.initialize(context)

            def on_market_event(self, market, portfolio, state):
                observed.append(portfolio)
                return delegate.on_market_event(market, portfolio, state)

        return Runtime()

    return replace(base, runtime_factory=factory)


def _run(model, observed):
    plugin = _observing_plugin(observed)
    registry = StrategyRegistry.build((plugin,))
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name,
        raw_parameters={"THRESHOLD_CLOSE_ABOVE": 100},
        fee_rate=0.001,
        slippage_bps=10,
    )
    return run_common_simulation_backtest(
        plugin=plugin,
        dataset=_data(),
        parameter_values={"THRESHOLD_CLOSE_ABOVE": 100},
        fee_rate=0.001,
        slippage_bps=10,
        execution_model=model,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open"
        ),
        portfolio_policy=legacy_research_portfolio_policy(),
        compiled_contract=compiled,
    )


def test_strategy_sees_actual_average_cost_after_partial_fill():
    observed = []
    run = _run(
        StressExecutionModel(
            0.001, 10, partial_fill_rate=1, partial_fill_fraction=0.5, seed=1
        ),
        observed,
    )
    filled = next(view for view in observed if view.filled_position_qty > 0)
    assert filled.filled_position_qty == run.ledger_entries[0].qty
    assert (
        filled.average_cost
        == run.ledger_entries[0].cost_basis_after
        / run.ledger_entries[0].asset_qty_after
    )


def test_fill_failure_is_visible_to_next_strategy_decision():
    observed = []
    _run(StressExecutionModel(0.001, 10, order_failure_rate=1, seed=1), observed)
    assert any(
        view.last_execution_status == "failed" and view.filled_position_qty == 0
        for view in observed
    )
