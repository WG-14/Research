from __future__ import annotations

import math

from bithumb_research.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_research.research.experiment_manifest import DateRange, legacy_research_portfolio_policy
from bithumb_research.research.strategies.sma_with_filter_kernel import run_sma_with_filter_backtest

from tests.research_sma_success_fixture import PRICES


def test_sma_research_kernel_matches_fixed_behavior_baseline() -> None:
    dataset = DatasetSnapshot("sma_equivalence", "fixture", "KRW-BTC", "1m", "validation", DateRange("2026-01-01", "2026-01-01"), tuple(Candle(index * 60_000, price, price, price, price, 1.0) for index, price in enumerate(PRICES)))
    result = run_sma_with_filter_backtest(dataset=dataset, parameter_values={"SMA_SHORT": 2, "SMA_LONG": 3, "SMA_FILTER_GAP_MIN_RATIO": 0.0, "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0, "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": 1.0, "SMA_COST_EDGE_ENABLED": False, "SMA_MARKET_REGIME_ENABLED": False, "ENTRY_EDGE_BUFFER_RATIO": 0.0, "STRATEGY_MIN_EXPECTED_EDGE_RATIO": 0.0, "LIVE_FEE_RATE_ESTIMATE": 0.0, "STRATEGY_EXIT_RULES": "stop_loss,opposite_cross,max_holding_time", "STRATEGY_EXIT_STOP_LOSS_RATIO": 0.01, "STRATEGY_EXIT_MAX_HOLDING_MIN": 0, "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO": 0.0, "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO": 0.0}, fee_rate=0.001, slippage_bps=10.0, portfolio_policy=legacy_research_portfolio_policy())
    decisions = [(item["candle_ts"], item["raw_signal"], item["entry_signal"], item["final_signal"], item["exit_rule"]) for item in result.decisions]
    assert decisions == [(180000, "HOLD", "HOLD", "HOLD", ""), (240000, "BUY", "BUY", "BUY", ""), (300000, "HOLD", "HOLD", "HOLD", ""), (360000, "HOLD", "HOLD", "SELL", "stop_loss"), (420000, "SELL", "SELL", "HOLD", ""), (480000, "HOLD", "HOLD", "HOLD", ""), (540000, "HOLD", "HOLD", "HOLD", ""), (600000, "BUY", "BUY", "BUY", ""), (660000, "HOLD", "HOLD", "HOLD", "")]
    assert [(trade["side"], trade["ts"], trade.get("exit_rule")) for trade in result.trades] == [("BUY", 240000, None), ("SELL", 360000, "stop_loss"), ("BUY", 600000, None)]
    assert math.isclose(result.trades[0]["price"], 11.011)
    assert math.isclose(result.trades[1]["fee"], 897.3035964035964)
    assert math.isclose(result.metrics.return_pct, 8.37232689011891)
    assert math.isclose(result.metrics.max_drawdown_pct, 16.678510179717698)
    assert result.metrics.win_rate == 0.0
