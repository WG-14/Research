import pytest
from market_research.research.backtest_types import BacktestResourceLimitExceeded, BacktestResourceLimits, BacktestRunContext, MemorySample
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy
from tests.test_common_simulation_engine import _dataset


def test_runtime_limit_is_checked_during_event_loop():
    context = BacktestRunContext(resource_limits=BacktestResourceLimits(max_runtime_s_per_candidate_split=0))
    with pytest.raises(BacktestResourceLimitExceeded, match="runtime_limit"):
        run_common_simulation_backtest(plugin=resolve_research_strategy("noop_baseline"), dataset=_dataset(),
            parameter_values={}, fee_rate=.001, slippage_bps=10, context=context)


def test_failed_run_completes_audit_with_failed_status():
    from tests.test_common_engine_audit_e2e import Sink
    sink = Sink()
    context = BacktestRunContext(audit_trace=sink,
        resource_limits=BacktestResourceLimits(max_runtime_s_per_candidate_split=0))
    with pytest.raises(BacktestResourceLimitExceeded) as caught:
        run_common_simulation_backtest(plugin=resolve_research_strategy("noop_baseline"), dataset=_dataset(),
            parameter_values={}, fee_rate=0, slippage_bps=0, context=context)
    assert sink.status == "failed"
    assert caught.value.evidence["audit_trace_index"]["completion_status"] == "failed"


def test_memory_limit_is_checked_during_event_loop():
    samples = iter((MemorySample(10, 10, "test"), MemorySample(20, 20, "test")))
    context = BacktestRunContext(resource_limits=BacktestResourceLimits(max_rss_mb=1),
                                 memory_sampler=lambda: next(samples))
    with pytest.raises(BacktestResourceLimitExceeded, match="memory_limit"):
        run_common_simulation_backtest(plugin=resolve_research_strategy("noop_baseline"), dataset=_dataset(),
            parameter_values={}, fee_rate=0, slippage_bps=0, context=context)


def test_trade_limit_fails_with_structured_evidence():
    context = BacktestRunContext(resource_limits=BacktestResourceLimits(max_trades=0))
    with pytest.raises(BacktestResourceLimitExceeded, match="trade_limit") as caught:
        run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"),
            dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 1},
            fee_rate=0, slippage_bps=0, context=context)
    assert caught.value.evidence["trade_count"] == 1
    assert caught.value.evidence["event_number"] >= 1
