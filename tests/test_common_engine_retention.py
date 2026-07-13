from market_research.research.backtest_types import BacktestResourceLimits, BacktestRunContext
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_catalog import resolve_research_strategy
from tests.test_common_simulation_engine import _dataset


def test_retention_limit_preserves_full_canonical_hash():
    plugin = resolve_research_strategy("noop_baseline")
    a = run_common_simulation_backtest(plugin=plugin, dataset=_dataset(), parameter_values={}, fee_rate=0, slippage_bps=0)
    b = run_common_simulation_backtest(plugin=plugin, dataset=_dataset(), parameter_values={}, fee_rate=0, slippage_bps=0,
        context=BacktestRunContext(resource_limits=BacktestResourceLimits(max_decisions_retained=1, max_equity_points_retained=1)))
    assert a.decision_stream_hash == b.decision_stream_hash
    assert a.execution_event_summary["ledger_stream_hash"] == b.execution_event_summary["ledger_stream_hash"]
    assert len(b.decisions) <= 1
    assert len(b.equity_curve) <= 1


def test_retained_projection_still_validates_authoritative_lineage():
    run = run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"),
        dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 1}, fee_rate=0, slippage_bps=0,
        context=BacktestRunContext(resource_limits=BacktestResourceLimits(max_decisions_retained=1)))
    run.validate_execution_lineage()
    assert len(run.decisions) == 1
    assert len(run.authoritative_decision_ids) > len(run.decisions)
