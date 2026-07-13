from __future__ import annotations

from market_research.research.backtest_types import BacktestRunContext
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.decision_stream_perturbation import EntrySignalOmissionTransformer
from market_research.research.experiment_manifest import DateRange, ExecutionTimingPolicy
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import builtin_strategy_registry


def test_entry_signal_omission_occurs_before_execution_requests() -> None:
    snapshot = DatasetSnapshot(
        "signal-omission",
        "fixture",
        "KRW-BTC",
        "1m",
        "validation",
        DateRange("2026-01-01", "2026-01-01"),
        tuple(
            Candle(index * 60_000, price, price, price, price, 1.0)
            for index, price in enumerate((100.0, 101.0, 102.0, 103.0))
        ),
    )
    transformer = EntrySignalOmissionTransformer(
        omission_rate_pct=100.0,
        seed_material={"manifest_hash": "sha256:" + "a" * 64, "candidate_id": "candidate"},
    )
    registry = builtin_strategy_registry()
    run = run_common_simulation_backtest(
        plugin=registry.resolve("buy_and_hold_baseline"),
        dataset=snapshot,
        parameter_values={"BUY_HOLD_BUY_INDEX": 0},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open",
            allow_same_candle_close_fill=False,
        ),
        context=BacktestRunContext(candidate_id="candidate", split_name="validation"),
        registry=registry,
        decision_stream_transformer=transformer,
    )

    evidence = run.execution_event_summary["decision_stream_perturbation_evidence"]
    assert evidence["layer"] == "decision_stream_pre_execution"
    assert evidence["observed_entry_signal_count"] == 1
    assert evidence["omitted_entry_signal_count"] == 1
    assert run.execution_requests == ()
    assert run.fills == ()
    assert run.metrics.return_pct == 0.0
