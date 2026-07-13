from dataclasses import replace
import pytest
from market_research.research.dataset_snapshot import Candle
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_catalog import resolve_research_strategy
from tests.test_common_simulation_engine import _dataset


@pytest.mark.parametrize(("name", "parameters"), [("noop_baseline", {}), ("buy_and_hold_baseline", {"BUY_HOLD_BUY_INDEX": 1})])
def test_future_suffix_change_does_not_change_prior_decisions(name, parameters):
    data = _dataset(); candles = list(data.candles); last = candles[-1]
    candles[-1] = Candle(last.ts, last.open, last.high, last.low, last.close * 5, last.volume)
    changed = replace(data, candles=tuple(candles))
    plugin = resolve_research_strategy(name)
    first = run_common_simulation_backtest(plugin=plugin, dataset=data, parameter_values=parameters, fee_rate=0, slippage_bps=0)
    second = run_common_simulation_backtest(plugin=plugin, dataset=changed, parameter_values=parameters, fee_rate=0, slippage_bps=0)
    assert [d.decision_id() for d in first.decisions[:-1]] == [d.decision_id() for d in second.decisions[:-1]]
