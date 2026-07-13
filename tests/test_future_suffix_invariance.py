from dataclasses import replace
import pytest
from market_research.research.dataset_snapshot import Candle
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy
from tests.test_common_simulation_engine import _dataset
from market_research.orderbook_depth_store import build_orderbook_depth_snapshot


@pytest.mark.parametrize(("name", "parameters"), [("noop_baseline", {}), ("buy_and_hold_baseline", {"BUY_HOLD_BUY_INDEX": 1})])
def test_future_suffix_change_does_not_change_prior_decisions(name, parameters):
    data = _dataset(); candles = list(data.candles); last = candles[-1]
    candles[-1] = Candle(last.ts, last.open, last.high, last.low, last.close * 5, last.volume)
    changed = replace(data, candles=tuple(candles))
    plugin = resolve_research_strategy(name)
    first = run_common_simulation_backtest(plugin=plugin, dataset=data, parameter_values=parameters, fee_rate=0, slippage_bps=0)
    second = run_common_simulation_backtest(plugin=plugin, dataset=changed, parameter_values=parameters, fee_rate=0, slippage_bps=0)
    assert [d.decision_id() for d in first.decisions[:-1]] == [d.decision_id() for d in second.decisions[:-1]]


@pytest.mark.parametrize(("name", "parameters"), [
    ("sma_with_filter", {"SMA_SHORT": 1, "SMA_LONG": 2}),
    ("threshold_research_only", {"THRESHOLD_CLOSE_ABOVE": 101}),
])
def test_stateful_strategy_future_suffix_invariance(name, parameters):
    data = _dataset(); candles = list(data.candles); last = candles[-1]
    candles[-1] = Candle(last.ts, last.open, last.high, last.low, last.close * 10, last.volume)
    changed = replace(data, candles=tuple(candles))
    plugin = resolve_research_strategy(name)
    first = run_common_simulation_backtest(plugin=plugin, dataset=data, parameter_values=parameters,
                                           fee_rate=0, slippage_bps=0)
    second = run_common_simulation_backtest(plugin=plugin, dataset=changed, parameter_values=parameters,
                                            fee_rate=0, slippage_bps=0)
    cutoff = last.ts
    assert [d.decision_id() for d in first.decisions if d.candle_ts < cutoff] == [
        d.decision_id() for d in second.decisions if d.candle_ts < cutoff]


def test_future_depth_change_does_not_change_prior_decisions():
    data = _dataset()
    depth_a = build_orderbook_depth_snapshot(ts=data.candles[-1].ts, pair="KRW-BTC",
        bid_levels=[(100, 1)], ask_levels=[(101, 1)], source="fixture")
    depth_b = build_orderbook_depth_snapshot(ts=data.candles[-1].ts, pair="KRW-BTC",
        bid_levels=[(1, 100)], ask_levels=[(1000, 100)], source="fixture")
    first_data = replace(data, orderbook_depth_snapshots=(depth_a,))
    second_data = replace(data, orderbook_depth_snapshots=(depth_b,))
    plugin = resolve_research_strategy("threshold_research_only")
    values = {"THRESHOLD_CLOSE_ABOVE": 101}
    first = run_common_simulation_backtest(plugin=plugin, dataset=first_data,
        parameter_values=values, fee_rate=0, slippage_bps=0)
    second = run_common_simulation_backtest(plugin=plugin, dataset=second_data,
        parameter_values=values, fee_rate=0, slippage_bps=0)
    cutoff = data.candles[-1].ts
    assert [d.decision_id() for d in first.decisions if d.candle_ts < cutoff] == [
        d.decision_id() for d in second.decisions if d.candle_ts < cutoff]
