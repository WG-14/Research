from __future__ import annotations

from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import DateRange, ExecutionTimingPolicy, legacy_research_portfolio_policy
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy


def test_sma_signal_stream_preserved_after_engine_migration() -> None:
    prices = (10, 9, 8, 9, 11, 10, 9, 10, 12)
    data = DatasetSnapshot("sma", "fixture", "KRW-BTC", "1m", "validation", DateRange("2026-01-01", "2026-01-01"), tuple(Candle(i * 60_000, p, p, p, p, 1) for i, p in enumerate(prices)))
    result = run_common_simulation_backtest(plugin=resolve_research_strategy("sma_with_filter"), dataset=data, parameter_values={"SMA_SHORT": 2, "SMA_LONG": 3, "SMA_FILTER_GAP_MIN_RATIO": 0, "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0, "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": 1, "SMA_COST_EDGE_ENABLED": False}, fee_rate=.001, slippage_bps=10, execution_model=FixedBpsExecutionModel(.001, 10), execution_timing_policy=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), portfolio_policy=legacy_research_portfolio_policy())
    assert any(item["raw_signal"] == "BUY" for item in result.decisions)
    assert all(fill.request_id for fill in result.fills)
