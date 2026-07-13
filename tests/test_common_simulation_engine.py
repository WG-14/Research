from __future__ import annotations

import pytest

from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import DateRange, ExecutionTimingPolicy, legacy_research_portfolio_policy
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy


def _dataset() -> DatasetSnapshot:
    return DatasetSnapshot("engine", "fixture", "KRW-BTC", "1m", "validation", DateRange("2026-01-01", "2026-01-01"), tuple(Candle(i * 60_000, 100 + i, 101 + i, 99 + i, 100 + i, 1.0) for i in range(5)))


class SpyModel(FixedBpsExecutionModel):
    def __init__(self) -> None:
        super().__init__(0.001, 10.0)
        self.requests = []

    def simulate(self, request):
        self.requests.append(request)
        return super().simulate(request)


def _run(model, *, timing: ExecutionTimingPolicy | None = None):
    return run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"), dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 1}, fee_rate=0.001, slippage_bps=10.0, execution_model=model, execution_timing_policy=timing or ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), portfolio_policy=legacy_research_portfolio_policy())


def test_each_order_intent_invokes_execution_model_exactly_once():
    spy = SpyModel(); run = _run(spy)
    assert len(run.order_intents) == len(spy.requests) == 1


def test_execution_model_exception_fails_candidate_evaluation():
    class Broken(SpyModel):
        def simulate(self, request): raise RuntimeError("model exploded")
    with pytest.raises(RuntimeError, match="model exploded"):
        _run(Broken())


def test_reference_failure_does_not_create_portfolio_fill():
    spy = SpyModel(); run = _run(spy, timing=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False))
    assert len(run.ledger_entries) == 1
    # Put the only signal on the final candle, where next-open cannot resolve.
    failed = run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"), dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 4}, fee_rate=.001, slippage_bps=10, execution_model=spy, execution_timing_policy=ExecutionTimingPolicy(fill_reference_policy="next_candle_open", allow_same_candle_close_fill=False), portfolio_policy=legacy_research_portfolio_policy())
    assert failed.fills[0].fill_status == "failed" and not failed.ledger_entries


def test_execution_request_contains_resolved_timing_fields():
    spy = SpyModel(); _run(spy)
    request = spy.requests[0]
    assert request.fill_reference_source == "next_candle_open"
    assert request.fill_reference_ts == 120_000
    assert request.reference_price == 102.0


def test_no_order_intent_does_not_invoke_execution_model():
    spy = SpyModel()
    run_common_simulation_backtest(plugin=resolve_research_strategy("noop_baseline"), dataset=_dataset(), parameter_values={}, fee_rate=.001, slippage_bps=10, execution_model=spy, execution_timing_policy=ExecutionTimingPolicy(), portfolio_policy=legacy_research_portfolio_policy())
    assert spy.requests == []
