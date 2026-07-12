from __future__ import annotations

from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import DateRange, ExecutionTimingPolicy, legacy_research_portfolio_policy
from market_research.research.execution_model import FixedBpsExecutionModel, StressExecutionModel
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_catalog import resolve_research_strategy


def _data() -> DatasetSnapshot:
    return DatasetSnapshot("threshold", "fixture", "KRW-BTC", "1m", "validation", DateRange("2026-01-01", "2026-01-01"), tuple(Candle(i * 60_000, p, p, p, p, 1) for i, p in enumerate((99, 100, 101, 102, 98, 105))))


def test_threshold_blocks_duplicate_entry_before_execution() -> None:
    run = run_common_simulation_backtest(plugin=resolve_research_strategy("threshold_research_only"), dataset=_data(), parameter_values={"THRESHOLD_CLOSE_ABOVE": 100}, fee_rate=.001, slippage_bps=10, execution_model=FixedBpsExecutionModel(.001, 10), execution_timing_policy=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), portfolio_policy=legacy_research_portfolio_policy())
    assert len(run.execution_requests) == 1


def test_threshold_failure_does_not_hold_position() -> None:
    run = run_common_simulation_backtest(plugin=resolve_research_strategy("threshold_research_only"), dataset=_data(), parameter_values={"THRESHOLD_CLOSE_ABOVE": 100}, fee_rate=.001, slippage_bps=10, execution_model=StressExecutionModel(.001, 10, order_failure_rate=1.0, seed=1), execution_timing_policy=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), portfolio_policy=legacy_research_portfolio_policy())
    assert not run.ledger_entries and all(fill.filled_qty == 0 for fill in run.fills)
