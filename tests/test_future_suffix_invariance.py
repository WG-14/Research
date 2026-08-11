from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from market_research.research.dataset_snapshot import Candle
from market_research.research.execution_model import StressExecutionModel
from market_research.research.experiment_manifest import (
    DateRange,
    ExecutionScenario,
    ExecutionTimingPolicy,
    StressSignalOmissionContract,
    StressSuiteContract,
    legacy_research_portfolio_policy,
)
from market_research.research.backtest_types import BacktestRunContext
from market_research.research.risk_contract import ResearchRiskPolicy
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.validation_protocol import (
    _execution_model_from_scenario,
    _seed_context,
    _signal_omission_stress_runs,
)
from market_research.research_composition import (
    builtin_strategy_registry,
    resolve_builtin_strategy as resolve_research_strategy,
)
from market_research.research.strategy_compiler import StrategyCompiler
from tests.test_common_simulation_engine import _dataset
from market_research.orderbook_depth_store import build_orderbook_depth_snapshot


def _stochastic_threshold_run(dataset):
    scenario = ExecutionScenario(
        type="stress",
        fee_rate=0.0,
        slippage_bps=0.0,
        partial_fill_rate=0.6,
        seed=17,
    )
    execution_model = _execution_model_from_scenario(
        scenario,
        seed_context=_seed_context(
            causal_execution_seed_scope_hash="sha256:" + "a" * 64,
            scenario=scenario,
            scenario_id="scenario_future_suffix_invariance",
            parameter_candidate_id="candidate_future_suffix_invariance",
            split_name=dataset.split_name,
        ),
    )
    return run_common_simulation_backtest(
        plugin=resolve_research_strategy("threshold_research_only"),
        dataset=dataset,
        parameter_values={"THRESHOLD_CLOSE_ABOVE": 101},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_model=execution_model,
    )


@pytest.mark.parametrize(
    ("name", "parameters"),
    [("noop_baseline", {}), ("buy_and_hold_baseline", {"BUY_HOLD_BUY_INDEX": 1})],
)
def test_future_suffix_change_does_not_change_prior_decisions(name, parameters):
    data = _dataset()
    candles = list(data.candles)
    last = candles[-1]
    candles[-1] = Candle(
        last.ts, last.open, last.high, last.low, last.close * 5, last.volume
    )
    changed = replace(data, candles=tuple(candles))
    plugin = resolve_research_strategy(name)
    first = run_common_simulation_backtest(
        plugin=plugin,
        dataset=data,
        parameter_values=parameters,
        fee_rate=0,
        slippage_bps=0,
    )
    second = run_common_simulation_backtest(
        plugin=plugin,
        dataset=changed,
        parameter_values=parameters,
        fee_rate=0,
        slippage_bps=0,
    )
    assert [d.decision_id() for d in first.decisions[:-1]] == [
        d.decision_id() for d in second.decisions[:-1]
    ]


@pytest.mark.parametrize(
    ("name", "parameters"),
    [
        ("sma_with_filter", {"SMA_SHORT": 1, "SMA_LONG": 2}),
        ("threshold_research_only", {"THRESHOLD_CLOSE_ABOVE": 101}),
    ],
)
def test_stateful_strategy_future_suffix_invariance(name, parameters):
    data = _dataset()
    candles = list(data.candles)
    last = candles[-1]
    candles[-1] = Candle(
        last.ts, last.open, last.high, last.low, last.close * 10, last.volume
    )
    changed = replace(data, candles=tuple(candles))
    plugin = resolve_research_strategy(name)
    first = run_common_simulation_backtest(
        plugin=plugin,
        dataset=data,
        parameter_values=parameters,
        fee_rate=0,
        slippage_bps=0,
    )
    second = run_common_simulation_backtest(
        plugin=plugin,
        dataset=changed,
        parameter_values=parameters,
        fee_rate=0,
        slippage_bps=0,
    )
    cutoff = last.ts
    assert [d.decision_id() for d in first.decisions if d.candle_ts < cutoff] == [
        d.decision_id() for d in second.decisions if d.candle_ts < cutoff
    ]


def test_future_depth_change_does_not_change_prior_decisions():
    data = _dataset()
    depth_a = build_orderbook_depth_snapshot(
        ts=data.candles[-1].ts,
        pair="KRW-BTC",
        bid_levels=[(100, 1)],
        ask_levels=[(101, 1)],
        source="fixture",
    )
    depth_b = build_orderbook_depth_snapshot(
        ts=data.candles[-1].ts,
        pair="KRW-BTC",
        bid_levels=[(1, 100)],
        ask_levels=[(1000, 100)],
        source="fixture",
    )
    first_data = replace(data, orderbook_depth_snapshots=(depth_a,))
    second_data = replace(data, orderbook_depth_snapshots=(depth_b,))
    plugin = resolve_research_strategy("threshold_research_only")
    values = {"THRESHOLD_CLOSE_ABOVE": 101}
    first = run_common_simulation_backtest(
        plugin=plugin,
        dataset=first_data,
        parameter_values=values,
        fee_rate=0,
        slippage_bps=0,
    )
    second = run_common_simulation_backtest(
        plugin=plugin,
        dataset=second_data,
        parameter_values=values,
        fee_rate=0,
        slippage_bps=0,
    )
    cutoff = data.candles[-1].ts
    assert [d.decision_id() for d in first.decisions if d.candle_ts < cutoff] == [
        d.decision_id() for d in second.decisions if d.candle_ts < cutoff
    ]


def test_stochastic_execution_ignores_noncausal_content_hash_identity():
    data = _dataset()
    changed = replace(
        data,
        source_content_hash="sha256:" + "b" * 64,
        artifact_content_hash="sha256:" + "b" * 64,
    )
    assert data.snapshot_fingerprint_hash() != changed.snapshot_fingerprint_hash()

    first = _stochastic_threshold_run(data)
    second = _stochastic_threshold_run(changed)

    assert [item.decision_id() for item in first.decisions] == [
        item.decision_id() for item in second.decisions
    ]
    assert [item.derived_seed_hash for item in first.fills] == [
        item.derived_seed_hash for item in second.fills
    ]
    assert [item.fill_status for item in first.fills] == [
        item.fill_status for item in second.fills
    ]
    assert [item.filled_qty for item in first.fills] == [
        item.filled_qty for item in second.fills
    ]
    assert first.metrics == second.metrics


def test_stochastic_execution_future_row_suffix_preserves_prior_fills():
    data = _dataset()
    last = data.candles[-1]
    extended = replace(
        data,
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=(
            *data.candles,
            Candle(
                last.ts + 60_000,
                last.open,
                last.high,
                last.low,
                last.close,
                last.volume,
            ),
        ),
    )
    first = _stochastic_threshold_run(data)
    second = _stochastic_threshold_run(extended)

    assert [item.decision_id() for item in first.decisions] == [
        item.decision_id() for item in second.decisions[: len(first.decisions)]
    ]
    assert [item.derived_seed_hash for item in first.fills] == [
        item.derived_seed_hash for item in second.fills[: len(first.fills)]
    ]
    assert [item.fill_status for item in first.fills] == [
        item.fill_status for item in second.fills[: len(first.fills)]
    ]
    assert [item.filled_qty for item in first.fills] == [
        item.filled_qty for item in second.fills[: len(first.fills)]
    ]
    assert first.fills[0].fill_status == "partial"


def test_official_signal_omission_future_suffix_preserves_prior_omissions():
    data = _dataset()
    last = data.candles[-1]
    extended = replace(
        data,
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=(
            *data.candles,
            Candle(
                last.ts + 60_000,
                last.open,
                last.high,
                last.low,
                last.close,
                last.volume,
            ),
        ),
    )
    contract = StressSignalOmissionContract(
        omission_rates_pct=(50.0,),
        seed_policy=(
            "derived_from_causal_decision_id_candidate_scenario_split_contract_hash"
        ),
        min_return_retention_pct=0.0,
    )
    manifest = SimpleNamespace(
        experiment_id="signal-omission-future-suffix",
        stress_suite=StressSuiteContract(
            required_for_validation=False,
            signal_omission=contract,
        ),
        causal_execution_seed_scope_hash=lambda: "sha256:" + "a" * 64,
        execution_timing=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open",
            allow_same_candle_close_fill=False,
        ),
        portfolio_policy=legacy_research_portfolio_policy(),
        risk_policy=ResearchRiskPolicy(),
    )
    scenario = ExecutionScenario(
        type="stress",
        fee_rate=0.0,
        slippage_bps=0.0,
        partial_fill_rate=0.6,
        seed=17,
    )
    registry = builtin_strategy_registry()
    plugin = registry.resolve("buy_and_hold_baseline")
    parameters = {"BUY_HOLD_BUY_INDEX": 0}
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name,
        raw_parameters=parameters,
        fee_rate=scenario.fee_rate,
        slippage_bps=scenario.slippage_bps,
        context=BacktestRunContext(
            experiment_id=manifest.experiment_id,
            candidate_id="candidate-signal-omission",
            scenario_id="scenario-signal-omission",
            split_name=data.split_name,
        ),
    )

    def run(snapshot):
        return _signal_omission_stress_runs(
            manifest=manifest,
            snapshot=snapshot,
            scenario=scenario,
            scenario_id="scenario-signal-omission",
            scenario_index=0,
            candidate_id="candidate-signal-omission",
            parameter_values=parameters,
            plugin=plugin,
            registry=registry,
            compiled_contract=compiled,
        )

    original, suffixed = run(data), run(extended)
    assert len(original) == len(suffixed) == 1
    original_evidence = original[0]["decision_stream_perturbation_evidence"]
    suffixed_evidence = suffixed[0]["decision_stream_perturbation_evidence"]
    assert original_evidence == suffixed_evidence
    assert original_evidence["observed_entry_signal_count"] == 1
    assert original_evidence["seed_material_hash"].startswith("sha256:")


@pytest.mark.parametrize(
    ("seed_inputs", "reason"),
    [
        (cast(Any, []), "seed_scope_mapping_required"),
        ({}, "causal_seed_scope_fields_required"),
        (
            {
                "seed_policy": "legacy_manifest_scoped",
                "causal_execution_seed_scope_hash": "sha256:" + "a" * 64,
                "scenario_id": "scenario",
                "scenario_hash": "sha256:" + "b" * 64,
                "candidate_id": "candidate",
                "split_name": "validation",
            },
            "causal_seed_policy_required",
        ),
        (
            {
                "seed_policy": "causal_execution_request_scoped_v1",
                "causal_execution_seed_scope_hash": "not-a-hash",
                "scenario_id": "scenario",
                "scenario_hash": "sha256:" + "b" * 64,
                "candidate_id": "candidate",
                "split_name": "validation",
            },
            "causal_execution_seed_scope_hash_invalid",
        ),
        (
            {
                "seed_policy": "causal_execution_request_scoped_v1",
                "causal_execution_seed_scope_hash": "sha256:" + "a" * 64,
                "scenario_id": "scenario",
                "scenario_hash": "sha256:" + "b" * 64,
                "candidate_id": "candidate",
                "split_name": "validation",
                "manifest_hash": "sha256:" + "c" * 64,
            },
            "noncausal_seed_scope_fields:manifest_hash",
        ),
    ],
)
def test_stress_execution_rejects_noncausal_or_incomplete_seed_scope(
    seed_inputs: Any,
    reason: str,
):
    with pytest.raises(ValueError, match=reason):
        StressExecutionModel(
            fee_rate=0.0,
            slippage_bps=0.0,
            partial_fill_rate=0.5,
            seed=1,
            seed_derivation_inputs=seed_inputs,
        )
