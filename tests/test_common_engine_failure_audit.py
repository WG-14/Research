import pytest

from market_research.research.backtest_types import BacktestRun, BacktestRunContext
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy
from tests.test_common_engine_audit_e2e import Sink
from tests.test_common_simulation_engine import _dataset


def test_post_loop_lineage_failure_marks_audit_failed(monkeypatch):
    sink = Sink()
    monkeypatch.setattr(BacktestRun, "validate_execution_lineage",
                        lambda self: (_ for _ in ()).throw(ValueError("forced_lineage_failure")))
    with pytest.raises(ValueError, match="forced_lineage_failure") as caught:
        run_common_simulation_backtest(plugin=resolve_research_strategy("noop_baseline"),
            dataset=_dataset(), parameter_values={}, fee_rate=0, slippage_bps=0,
            context=BacktestRunContext(audit_trace=sink))
    assert sink.status == "failed"
    assert caught.value.audit_trace_index["completion_status"] == "failed"
