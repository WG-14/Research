from market_research.research.backtest_types import BacktestHeartbeatPolicy, BacktestRunContext
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_catalog import resolve_research_strategy
from tests.test_common_simulation_engine import _dataset


def test_heartbeat_callback_uses_common_schema_during_no_event_periods():
    rows = []
    run_common_simulation_backtest(plugin=resolve_research_strategy("noop_baseline"),
        dataset=_dataset(), parameter_values={"NOOP_DECISION_START_INDEX": 99}, fee_rate=0, slippage_bps=0,
        context=BacktestRunContext(heartbeat=BacktestHeartbeatPolicy(bar_interval=1),
                                   progress_callback=rows.append))
    assert len(rows) == len(_dataset().candles)
    assert all(set(("stage", "bar_count", "candidate_id", "scenario_id", "split")) <= set(row)
               and row["stage"] == "heartbeat" for row in rows)
