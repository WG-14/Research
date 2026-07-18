import pytest
from dataclasses import replace

from market_research.research.backtest_types import BacktestRun, BacktestRunContext
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_common_engine_audit_e2e import Sink
from tests.test_common_simulation_engine import _dataset


def test_post_loop_lineage_failure_marks_audit_failed(monkeypatch):
    sink = Sink()
    monkeypatch.setattr(
        BacktestRun,
        "validate_execution_lineage",
        lambda self: (_ for _ in ()).throw(ValueError("forced_lineage_failure")),
    )
    with pytest.raises(ValueError, match="forced_lineage_failure") as caught:
        run_common_simulation_backtest(
            plugin=resolve_research_strategy("noop_baseline"),
            dataset=_dataset(),
            parameter_values={},
            fee_rate=0,
            slippage_bps=0,
            context=BacktestRunContext(audit_trace=sink),
        )
    assert sink.status == "failed"
    assert caught.value.audit_trace_index["completion_status"] == "failed"


def test_invalid_strategy_output_fails_only_that_run_and_next_strategy_succeeds():
    base = resolve_research_strategy("noop_baseline")

    def invalid_output(**_kwargs):
        return ({"not": "a decision event"},)

    invalid = replace(base, event_builder=invalid_output, runtime_factory=None)
    with pytest.raises((AttributeError, TypeError, ValueError)):
        run_common_simulation_backtest(
            plugin=invalid,
            dataset=_dataset(),
            parameter_values={},
            fee_rate=0,
            slippage_bps=0,
        )

    healthy = run_common_simulation_backtest(
        plugin=base,
        dataset=_dataset(),
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
    )
    assert healthy.compiled_strategy_contract is not None
    assert healthy.compiled_strategy_contract.strategy_name == "noop_baseline"
    assert not healthy.trades


def test_strategy_exception_does_not_mutate_shared_dataset_or_registry():
    base = resolve_research_strategy("noop_baseline")
    dataset = _dataset()
    before = tuple(dataset.candles)

    def explode(**_kwargs):
        raise RuntimeError("strategy-local-failure")

    failing = replace(base, event_builder=explode, runtime_factory=None)
    with pytest.raises(RuntimeError, match="strategy-local-failure"):
        run_common_simulation_backtest(
            plugin=failing,
            dataset=dataset,
            parameter_values={},
            fee_rate=0,
            slippage_bps=0,
        )

    assert tuple(dataset.candles) == before
    assert (
        resolve_research_strategy("noop_baseline").contract_hash()
        == base.contract_hash()
    )
