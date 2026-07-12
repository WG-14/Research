from __future__ import annotations

import pytest

from market_research.research.execution_evidence import ExecutionEvidenceError, validate_execution_evidence
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import ExecutionTimingPolicy
from tests.test_common_simulation_engine import SpyModel, _run


def test_validation_fails_when_executed_model_hash_differs():
    model = SpyModel(); run = _run(model)
    run.execution_event_summary["executed_execution_model_hash"] = "sha256:wrong"  # type: ignore[index]
    with pytest.raises(ExecutionEvidenceError, match="model_hash_mismatch"):
        validate_execution_evidence(run=run, timing=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), model=model)


def test_zero_intent_run_allows_zero_execution_counts():
    from market_research.research.simulation_engine import run_common_simulation_backtest
    from market_research.research.strategy_catalog import resolve_research_strategy
    from tests.test_common_simulation_engine import _dataset
    model = FixedBpsExecutionModel(.001, 10)
    run = run_common_simulation_backtest(plugin=resolve_research_strategy("noop_baseline"), dataset=_dataset(), parameter_values={}, fee_rate=.001, slippage_bps=10, execution_model=model, execution_timing_policy=ExecutionTimingPolicy(), portfolio_policy=__import__("market_research.research.experiment_manifest", fromlist=["legacy_research_portfolio_policy"]).legacy_research_portfolio_policy())
    assert validate_execution_evidence(run=run, timing=ExecutionTimingPolicy(), model=model)["status"] == "PASS"
