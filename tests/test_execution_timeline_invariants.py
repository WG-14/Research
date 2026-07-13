from __future__ import annotations

import pytest

from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import DateRange, ExecutionTimingPolicy
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy
from tests.test_common_simulation_engine import SpyModel, _dataset, _run


def test_next_open_fill_occurs_after_close_decision():
    run = _run(SpyModel())
    fill = run.fills[0]
    assert fill.decision_ts <= fill.submit_ts_assumption <= fill.fill_reference_ts <= fill.portfolio_effective_ts
    assert fill.fill_reference_source == "next_candle_open"


def test_legacy_trade_ts_is_marked_as_non_authoritative_alias():
    run = _run(SpyModel())
    assert run.trades[0]["event_ts_role"] == "signal_ts_legacy_non_authoritative"


def test_default_timing_never_fills_a_close_signal_at_the_same_close():
    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
        dataset=_dataset(),
        parameter_values={"BUY_HOLD_BUY_INDEX": 1},
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    fill = run.fills[0]
    assert fill.fill_reference_policy == "next_candle_open"
    assert fill.fill_reference_source == "next_candle_open"
    assert fill.fill_reference_ts > fill.signal_candle_start_ts
    assert fill.allow_same_candle_close_fill is False


def test_next_open_gap_fill_is_not_marked_with_the_previous_close():
    dataset = DatasetSnapshot(
        "engine", "gap-fixture", "KRW-BTC", "1m", "validation",
        DateRange("2026-01-01", "2026-01-01"),
        (
            Candle(0, 100, 100, 100, 100, 1),
            Candle(60_000, 200, 200, 200, 200, 1),
            Candle(120_000, 200, 200, 200, 200, 1),
        ),
    )
    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"), dataset=dataset,
        parameter_values={"BUY_HOLD_BUY_INDEX": 0}, fee_rate=0.0, slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(),
    )
    assert run.fills[0].avg_fill_price == 200.0
    assert run.equity_curve[0].equity == pytest.approx(1_000_000.0)
    assert run.equity_curve[0].asset_qty == 0.0
    assert run.equity_curve[0].mark_price == 100.0
    assert run.equity_curve[0].mark_price_source == "candle_close"
    assert run.equity_curve[1].equity == pytest.approx(1_000_000.0)
    assert run.metrics.max_drawdown_pct == pytest.approx(0.0)
