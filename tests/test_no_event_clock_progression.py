from market_research.research.backtest_types import (
    BacktestHeartbeatPolicy,
    BacktestRunContext,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_common_simulation_engine import _dataset


def test_no_event_candle_still_records_equity_and_heartbeat():
    heartbeats = []
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("noop_baseline"),
        dataset=_dataset(),
        parameter_values={"NOOP_DECISION_START_INDEX": 99},
        fee_rate=0,
        slippage_bps=0,
        context=BacktestRunContext(
            heartbeat=BacktestHeartbeatPolicy(bar_interval=1),
            progress_callback=heartbeats.append,
        ),
    )
    assert len(run.decisions) == 0
    assert len(run.equity_curve) == len(_dataset().candles)
    assert len(heartbeats) == len(_dataset().candles)
    assert all(item["stage"] == "heartbeat" for item in heartbeats)


def test_each_candle_has_exactly_one_equity_point():
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("noop_baseline"),
        dataset=_dataset(),
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
    )
    assert len(run.equity_curve) == len(_dataset().candles)
    assert len({point.ts for point in run.equity_curve}) == len(_dataset().candles)
