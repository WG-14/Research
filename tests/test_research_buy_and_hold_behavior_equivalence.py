from __future__ import annotations

from market_research.research.backtest_engine import run_buy_and_hold_baseline_backtest
from market_research.research_composition import builtin_strategy_registry
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import (
    DateRange,
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.execution_model import FixedBpsExecutionModel


def test_buy_and_hold_emits_one_intent_and_uses_next_open() -> None:
    data = DatasetSnapshot(
        "buy",
        "fixture",
        "KRW-BTC",
        "1m",
        "validation",
        DateRange("2026-01-01", "2026-01-01"),
        tuple(
            Candle(i * 60_000, 100 + i, 101 + i, 99 + i, 100 + i, 1) for i in range(5)
        ),
    )
    result = run_buy_and_hold_baseline_backtest(
        dataset=data,
        parameter_values={"BUY_HOLD_BUY_INDEX": 1},
        fee_rate=0.001,
        slippage_bps=10,
        execution_model=FixedBpsExecutionModel(0.001, 10),
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False
        ),
        portfolio_policy=legacy_research_portfolio_policy(),
        strategy_registry=builtin_strategy_registry(),
    )
    assert (
        len(result.order_intents)
        == len(result.execution_requests)
        == len(result.fills)
        == 1
    )
    assert result.fills[0].fill_reference_source == "next_candle_open"
    assert result.fills[0].fill_reference_ts == 120_000
