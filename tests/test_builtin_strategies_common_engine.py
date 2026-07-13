from __future__ import annotations

import pytest

from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import ExecutionTimingPolicy, legacy_research_portfolio_policy
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy
from tests.test_common_simulation_engine import SpyModel, _dataset


@pytest.mark.parametrize(("name", "parameters"), [
    ("buy_and_hold_baseline", {"BUY_HOLD_BUY_INDEX": 1}),
    ("threshold_research_only", {"THRESHOLD_CLOSE_ABOVE": 0}),
])
def test_all_builtin_trading_strategies_invoke_common_model(name, parameters):
    model = SpyModel()
    run = run_common_simulation_backtest(plugin=resolve_research_strategy(name), dataset=_dataset(), parameter_values=parameters, fee_rate=.001, slippage_bps=10, execution_model=model, execution_timing_policy=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), portfolio_policy=legacy_research_portfolio_policy())
    assert len(model.requests) == run.execution_event_summary["execution_model_invocation_count"]
    assert len(model.requests) <= len(run.order_intents)


def test_failed_reference_does_not_increment_model_invocation_count():
    model = FixedBpsExecutionModel(.001, 10)
    run = run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"), dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 4}, fee_rate=.001, slippage_bps=10, execution_model=model, execution_timing_policy=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), portfolio_policy=legacy_research_portfolio_policy())
    assert run.execution_event_summary["execution_model_invocation_count"] == 0
