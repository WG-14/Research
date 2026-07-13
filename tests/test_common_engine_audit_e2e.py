from market_research.research.backtest_types import BacktestHeartbeatPolicy, BacktestRunContext
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_catalog import resolve_research_strategy
from tests.test_common_simulation_engine import _dataset


class Sink:
    def __init__(self): self.decisions=[]; self.executions=[]; self.equity=[]; self.status=None
    def write_decision(self, value): self.decisions.append(value)
    def write_execution(self, value): self.executions.append(value)
    def write_equity(self, value): self.equity.append(value)
    def complete(self, status): self.status=status; return {"completion_status": status, "decision_row_count": len(self.decisions), "execution_row_count": len(self.executions), "equity_row_count": len(self.equity)}


def test_complete_external_audit_finishes_with_completed_index():
    sink, progress = Sink(), []
    run = run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"),
        dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 1}, fee_rate=.001, slippage_bps=10,
        context=BacktestRunContext(audit_trace=sink, heartbeat=BacktestHeartbeatPolicy(bar_interval=2), progress_callback=progress.append))
    assert run.audit_trace_index["completion_status"] == "completed"
    assert len(sink.decisions) == len(run.decisions)
    assert len(sink.executions) == len(run.fills)
    assert len(sink.equity) == len(run.equity_curve)
    assert progress and progress[0]["event"] == "heartbeat"
