from __future__ import annotations

import inspect
import math

from bithumb_bot.research import backtest_kernel
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import (
    DateRange,
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from bithumb_bot.research.strategies.threshold_research_only_events import (
    build_threshold_research_only_events,
)
from bithumb_bot.research.strategies.threshold_research_only_kernel import (
    run_threshold_research_only_backtest,
)

from tests.research_threshold_success_fixture import PRICES, THRESHOLD


def _dataset() -> DatasetSnapshot:
    return DatasetSnapshot(
        "threshold_equivalence",
        "fixture",
        "KRW-BTC",
        "1m",
        "validation",
        DateRange("2026-01-01", "2026-01-01"),
        tuple(
            Candle(index * 60_000, price, price, price, price, 1.0)
            for index, price in enumerate(PRICES)
        ),
    )


def test_threshold_research_kernel_matches_fixed_legacy_behavior_golden() -> None:
    dataset = _dataset()
    policy = legacy_research_portfolio_policy()
    events = build_threshold_research_only_events(
        dataset=dataset,
        parameter_values={"THRESHOLD_CLOSE_ABOVE": THRESHOLD},
        fee_rate=0.001,
        slippage_bps=10.0,
        execution_timing_policy=ExecutionTimingPolicy(),
        portfolio_policy=policy,
    )

    assert [
        (
            event.candle_ts,
            event.decision_ts,
            event.feature_snapshot["close"],
            event.raw_signal,
            event.entry_signal,
            event.exit_signal,
            event.final_signal,
            event.reason,
            event.strategy_diagnostics["close_above_threshold"],
        )
        for event in events
    ] == [
        (0, 60_000, 99.0, "HOLD", "HOLD", "HOLD", "HOLD", "threshold_not_met", False),
        (60_000, 120_000, 100.0, "HOLD", "HOLD", "HOLD", "HOLD", "threshold_not_met", False),
        (120_000, 180_000, 101.0, "BUY", "BUY", "HOLD", "BUY", "threshold_close_above", True),
        (180_000, 240_000, 102.0, "BUY", "BUY", "HOLD", "BUY", "threshold_close_above", True),
        (240_000, 300_000, 98.0, "HOLD", "HOLD", "HOLD", "HOLD", "threshold_not_met", False),
        (300_000, 360_000, 105.0, "BUY", "BUY", "HOLD", "BUY", "threshold_close_above", True),
    ]
    assert [event.order_intent is not None for event in events] == [False, False, True, True, False, True]

    result = run_threshold_research_only_backtest(
        dataset,
        {"THRESHOLD_CLOSE_ABOVE": THRESHOLD},
        0.001,
        10.0,
        portfolio_policy=policy,
    )

    assert [
        (item["raw_signal"], item["entry_signal"], item["exit_signal"], item["final_signal"], item["reason"])
        for item in result.decisions
    ] == [
        ("HOLD", "HOLD", "HOLD", "HOLD", "threshold_not_met"),
        ("HOLD", "HOLD", "HOLD", "HOLD", "threshold_not_met"),
        ("BUY", "BUY", "HOLD", "BUY", "none"),
        ("BUY", "BUY", "HOLD", "HOLD", "buy_blocked_existing_position_or_pending_buy"),
        ("HOLD", "HOLD", "HOLD", "HOLD", "threshold_not_met"),
        ("BUY", "BUY", "HOLD", "HOLD", "buy_blocked_existing_position_or_pending_buy"),
    ]
    assert [
        item["strategy_diagnostics"]["duplicate_entry_blocked"]
        for item in result.decisions
    ] == [False, False, False, True, False, True]
    assert [trade["side"] for trade in result.trades] == ["BUY"]
    trade = result.trades[0]
    assert trade["ts"] == 120_000
    assert trade["decision_ts"] == 180_000
    assert math.isclose(float(trade["reference_price"]), 101.0)
    assert math.isclose(float(trade["price"]), 101.101)
    assert math.isclose(float(trade["notional"]), 990_000.0)
    assert math.isclose(float(trade["fee"]), 990.0)
    assert math.isclose(float(trade["slippage"]), 988.0219780218303)
    assert math.isclose(float(trade["asset_qty"]), 9782.395821999784)
    assert math.isclose(float(trade["cash"]), 10_000.0)
    assert result.metrics_v2 is not None
    assert math.isclose(result.metrics_v2.return_risk.total_return_pct, 3.715156130997732)
    assert math.isclose(result.metrics.max_drawdown_pct, 3.88265662499069)
    assert result.metrics.trade_count == 0
    assert result.metrics_v2.trade_quality.execution_count == 1
    assert result.metrics_v2.trade_quality.closed_trade_count == 0
    assert result.metrics_v2.return_risk.open_position_at_end is True
    assert result.metrics_v2.return_risk.unrealized_pnl_end > 0.0
    assert result.position_intervals[-1].close_ts is None
    assert result.resource_usage["final_position_marked_to_market"] is True
    assert result.resource_usage["duplicate_entry_block_reason"] == "buy_blocked_existing_position_or_pending_buy"


def test_threshold_kernel_does_not_enter_common_kernel() -> None:
    source = inspect.getsource(run_threshold_research_only_backtest)

    assert "run_decision_event_backtest" not in source
    assert "strategy_plugins" not in source
    assert backtest_kernel.run_decision_event_backtest is not None
