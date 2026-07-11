from __future__ import annotations

import math

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import (
    DateRange,
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from bithumb_bot.research.strategies.buy_and_hold_baseline_events import (
    build_buy_and_hold_baseline_events,
)
from bithumb_bot.research.strategies.buy_and_hold_baseline_kernel import (
    run_buy_and_hold_baseline_backtest,
)

from tests.research_buy_and_hold_success_fixture import PRICES


def _dataset() -> DatasetSnapshot:
    return DatasetSnapshot(
        "buy_and_hold_equivalence",
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


def test_buy_and_hold_research_kernel_matches_fixed_legacy_behavior_golden() -> None:
    dataset = _dataset()
    policy = legacy_research_portfolio_policy()
    events = build_buy_and_hold_baseline_events(
        dataset=dataset,
        parameter_values={"BUY_HOLD_BUY_INDEX": 1, "BUY_HOLD_DECISION_REASON": "golden"},
        fee_rate=0.001,
        slippage_bps=10.0,
        execution_timing_policy=ExecutionTimingPolicy(),
        portfolio_policy=policy,
    )
    assert [
        (event.candle_ts, event.decision_ts, event.raw_signal, event.entry_signal, event.exit_signal)
        for event in events
    ] == [
        (0, 60_000, "HOLD", "HOLD", "HOLD"),
        (60_000, 120_000, "BUY", "BUY", "HOLD"),
        (120_000, 180_000, "HOLD", "HOLD", "HOLD"),
        (180_000, 240_000, "HOLD", "HOLD", "HOLD"),
        (240_000, 300_000, "HOLD", "HOLD", "HOLD"),
    ]

    result = run_buy_and_hold_baseline_backtest(
        dataset,
        {"BUY_HOLD_BUY_INDEX": 1, "BUY_HOLD_DECISION_REASON": "golden"},
        0.001,
        10.0,
        portfolio_policy=policy,
    )

    assert [trade["side"] for trade in result.trades] == ["BUY"]
    trade = result.trades[0]
    assert trade["ts"] == 60_000
    assert math.isclose(float(trade["reference_price"]), 110.0)
    assert math.isclose(float(trade["price"]), 110.11)
    assert math.isclose(float(trade["notional"]), 990_000.0)
    assert math.isclose(float(trade["fee"]), 990.0)
    assert math.isclose(float(trade["asset_qty"]), 8982.017982017984)
    assert math.isclose(float(trade["cash"]), 10_000.0)
    assert result.metrics_v2 is not None
    assert math.isclose(result.metrics_v2.return_risk.total_return_pct, 17.766233766233785)
    assert math.isclose(result.metrics.max_drawdown_pct, 18.161838161838148)
    assert result.metrics.trade_count == 0  # closed trades: no-exit is intentional
    assert result.metrics_v2.trade_quality.execution_count == 1
    assert result.metrics_v2.trade_quality.closed_trade_count == 0
    assert result.metrics_v2.return_risk.open_position_at_end is True
    assert math.isclose(result.metrics_v2.return_risk.unrealized_pnl_end, 177_662.3376623378)
    assert result.position_intervals[-1].close_ts is None
