from __future__ import annotations

from typing import cast

import pytest

from market_research.research import report_writer
from market_research.research.metrics_contract import (
    MS_PER_YEAR,
    ClosedTradeRecord,
    EquityPoint,
    ExecutionRecord,
    MetricContractV2,
    PositionInterval,
    build_metrics_v2,
)


HOUR_MS = 60 * 60 * 1000
YEAR_MS = int(MS_PER_YEAR)


def _complete_metrics() -> MetricContractV2:
    quarter = YEAR_MS // 4
    equity_curve = (
        EquityPoint(ts=0, equity=1_000.0, cash=1_000.0, asset_qty=0.0),
        EquityPoint(ts=quarter, equity=1_100.0, cash=100.0, asset_qty=10.0),
        EquityPoint(ts=quarter * 2, equity=880.0, cash=80.0, asset_qty=8.0),
        EquityPoint(ts=quarter * 3, equity=990.0, cash=90.0, asset_qty=9.0),
        EquityPoint(ts=YEAR_MS, equity=1_210.0, cash=110.0, asset_qty=10.0),
    )
    positions = (
        PositionInterval(open_ts=0, close_ts=HOUR_MS),
        PositionInterval(open_ts=2 * HOUR_MS, close_ts=4 * HOUR_MS),
        PositionInterval(open_ts=5 * HOUR_MS, close_ts=8 * HOUR_MS),
    )
    trades = (
        ClosedTradeRecord(
            entry_ts=0,
            exit_ts=HOUR_MS,
            holding_minutes=60.0,
            entry_notional=1_000.0,
            net_pnl=100.0,
            return_pct=10.0,
            mfe=120.0,
            mae=-20.0,
            mfe_pct=12.0,
            mae_pct=-2.0,
        ),
        ClosedTradeRecord(
            entry_ts=2 * HOUR_MS,
            exit_ts=4 * HOUR_MS,
            holding_minutes=120.0,
            entry_notional=1_000.0,
            net_pnl=-50.0,
            return_pct=-5.0,
            mfe=30.0,
            mae=-70.0,
            mfe_pct=3.0,
            mae_pct=-7.0,
        ),
        ClosedTradeRecord(
            entry_ts=5 * HOUR_MS,
            exit_ts=8 * HOUR_MS,
            holding_minutes=180.0,
            entry_notional=500.0,
            net_pnl=25.0,
            return_pct=2.5,
            mfe=20.0,
            mae=-5.0,
            mfe_pct=4.0,
            mae_pct=-1.0,
        ),
    )
    executions = (
        ExecutionRecord(
            side="BUY", status="filled", filled_qty=10.0, price=100.0, slippage=2.0
        ),
        ExecutionRecord(
            side="SELL",
            status="filled",
            filled_qty=10.0,
            price=110.0,
            slippage=3.0,
        ),
    )
    return build_metrics_v2(
        starting_cash=1_000.0,
        final_cash=1_210.0,
        final_asset_qty=0.0,
        final_mark_price=0.0,
        equity_curve=equity_curve,
        position_intervals=positions,
        closed_trades=trades,
        execution_records=executions,
        benchmark_period_returns=(0.05, -0.10, 0.05, 0.10),
    )


def test_v08_v09_metrics_are_computed_and_retained_by_compact_report() -> None:
    metrics = _complete_metrics()
    trade = metrics.trade_quality
    portfolio = metrics.portfolio

    assert trade.closed_trade_count == trade.as_dict()["trade_count"] == 3
    assert trade.win_rate == pytest.approx(2 / 3)
    assert trade.avg_win == pytest.approx(62.5)
    assert trade.avg_loss == pytest.approx(-50.0)
    assert trade.payoff_ratio == pytest.approx(1.25)
    assert trade.expectancy_per_trade_krw == pytest.approx(25.0)
    assert trade.expectancy_per_trade_pct == pytest.approx(2.5)
    assert trade.median_trade_return_pct == pytest.approx(2.5)
    assert trade.max_trade_return_pct == pytest.approx(10.0)
    assert trade.min_trade_return_pct == pytest.approx(-5.0)
    assert trade.avg_holding_time_ms == pytest.approx(2 * HOUR_MS)
    assert trade.avg_mfe_pct == pytest.approx(19 / 3)
    assert trade.avg_mae_pct == pytest.approx(-10 / 3)
    assert trade.slippage_total == pytest.approx(5.0)
    assert trade.net_expectancy_per_hour_krw == pytest.approx(12.5)
    assert trade.net_expectancy_per_capital_hour_pct == pytest.approx(75 / 45)

    assert portfolio.cumulative_return_pct == pytest.approx(21.0)
    assert portfolio.annualized_return_pct == pytest.approx(21.0)
    assert portfolio.max_drawdown_pct == pytest.approx(20.0)
    assert portfolio.max_drawdown_duration_ms == YEAR_MS // 4
    assert portfolio.recovery_duration_ms == YEAR_MS // 2
    assert portfolio.annualized_volatility_pct is not None
    assert portfolio.annualized_downside_deviation_pct is not None
    assert portfolio.market_exposure_pct == pytest.approx(6 * HOUR_MS / YEAR_MS * 100)
    assert portfolio.turnover_ratio == pytest.approx(2_100 / 1_036)
    assert portfolio.average_cash_usage_pct == pytest.approx(800 / 11)
    assert portfolio.peak_cash_usage_pct == pytest.approx(1_000 / 11)
    assert portfolio.max_concurrent_positions == 1
    assert portfolio.max_position_concentration_pct == pytest.approx(1_000 / 11)
    assert portfolio.beta is not None
    assert portfolio.value_at_risk_95_pct is not None
    assert portfolio.conditional_value_at_risk_95_pct is not None
    assert portfolio.sharpe_ratio is not None
    assert portfolio.sortino_ratio is not None
    assert portfolio.calmar_ratio == pytest.approx(1.05)

    payload = metrics.as_dict()
    compact = report_writer._compact_metrics_payload(payload)
    assert compact["trade_quality"]["net_expectancy_per_hour_krw"] == pytest.approx(
        12.5
    )
    assert compact["portfolio"]["max_drawdown_duration_ms"] == YEAR_MS // 4
    assert compact["portfolio"]["risk_adjusted_performance"]["calmar_ratio"] == (
        pytest.approx(1.05)
    )


def test_unavailable_metric_values_have_typed_path_specific_reasons() -> None:
    metrics = build_metrics_v2(
        starting_cash=1_000.0,
        final_cash=1_000.0,
        final_asset_qty=0.0,
        final_mark_price=0.0,
        equity_curve=(),
        position_intervals=(),
        closed_trades=(),
        execution_records=(),
    )
    payload = metrics.as_dict()
    availability = cast(dict[str, dict[str, str]], payload["metric_availability"])

    assert metrics.trade_quality.win_rate is None
    assert metrics.trade_quality.median_trade_return_pct is None
    assert metrics.portfolio.annualized_volatility_pct is None
    assert metrics.portfolio.beta is None
    assert metrics.portfolio.turnover_ratio is None
    assert metrics.portfolio.max_concurrent_positions == 0
    for path in (
        "trade_quality.win_rate",
        "trade_quality.median_trade_return_pct",
        "trade_quality.net_expectancy_per_hour_krw",
        "portfolio.annualized_volatility_pct",
        "portfolio.max_drawdown_duration_ms",
        "portfolio.turnover_ratio",
        "portfolio.beta",
    ):
        assert availability[path]["status"] == "unavailable"
        assert availability[path]["reason"]
