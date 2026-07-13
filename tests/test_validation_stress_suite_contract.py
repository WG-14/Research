from __future__ import annotations

import pytest

from market_research.research.experiment_manifest import (
    ExecutionModelConfig,
    ExecutionScenario,
    ManifestValidationError,
    StressSuiteContract,
    _parse_stress_suite,
    validation_execution_stress_policy_reasons,
)
from market_research.research.metrics_contract import ClosedTradeRecord
from market_research.research.stress_suite import (
    StressSuiteContext,
    analyze_stress_suite,
    analyze_trade_bootstrap_uncertainty,
    analyze_parameter_perturbation,
    analyze_signal_omission,
)


def _complete_stress_suite_payload() -> dict[str, object]:
    return {
        "required_for_validation": True,
        "trade_removal": {
            "top_n_by_net_pnl": [1, 3],
            "min_return_retention_pct": 50.0,
        },
        "trade_order_monte_carlo": {
            "iterations": 100,
            "seed_policy": "derived_from_manifest_candidate_scenario_split_hash",
            "min_survival_probability": 0.8,
            "ruin_max_drawdown_pct": 50.0,
            "min_closed_trades": 10,
        },
        "period_ablation": {
            "calendar_years": "auto",
            "min_pass_ratio": 0.8,
            "min_return_retention_pct": 50.0,
        },
        "parameter_perturbation": {
            "relative_pct": [-10.0, 10.0],
            "numeric_params_only": True,
            "min_pass_ratio": 0.8,
            "min_neighbor_trade_count_retention_pct": 50.0,
            "min_neighbor_return_retention_pct": 50.0,
            "min_connected_pass_region_size": 2,
            "max_normalized_local_curvature": 1.0,
        },
        "signal_omission": {
            "omission_rates_pct": [100.0],
            "seed_policy": "derived_from_manifest_candidate_scenario_split_contract_hash",
            "min_return_retention_pct": 0.0,
            "min_omitted_entry_signals": 1,
        },
    }


def test_validation_bound_stress_suite_rejects_empty_required_contract() -> None:
    with pytest.raises(ManifestValidationError, match="required components missing"):
        _parse_stress_suite(
            {"required_for_validation": True},
            research_classification="validated_candidate",
        )


def test_validation_bound_stress_suite_accepts_all_required_components() -> None:
    contract = _parse_stress_suite(
        _complete_stress_suite_payload(),
        research_classification="validated_candidate",
    )

    assert contract is not None
    assert contract.trade_removal is not None
    assert contract.trade_order_monte_carlo is not None
    assert contract.period_ablation is not None
    assert contract.parameter_perturbation is not None
    assert contract.signal_omission is not None


def test_parameter_surface_reports_connected_region_curvature_and_trade_retention() -> None:
    result = analyze_parameter_perturbation(
        contract={
            "relative_pct": [-10.0, 10.0],
            "min_pass_ratio": 1.0,
            "min_neighbor_trade_count_retention_pct": 50.0,
            "min_neighbor_return_retention_pct": 50.0,
            "min_connected_pass_region_size": 3,
            "max_normalized_local_curvature": 1.0,
        },
        base_parameter_values={"window": 10},
        candidates=(
            {
                "candidate_id": "low",
                "parameter_values": {"window": 9},
                "validation_metrics": {"return_pct": 9.0, "trade_count": 9},
                "scenario_acceptance_gate_result": "PASS",
            },
            {
                "candidate_id": "base",
                "parameter_values": {"window": 10},
                "validation_metrics": {"return_pct": 10.0, "trade_count": 10},
                "scenario_acceptance_gate_result": "PASS",
            },
            {
                "candidate_id": "high",
                "parameter_values": {"window": 11},
                "validation_metrics": {"return_pct": 8.0, "trade_count": 8},
                "scenario_acceptance_gate_result": "PASS",
            },
        ),
    )

    assert result["status"] == "PASS"
    assert result["connected_pass_region_size"] == 3
    assert result["local_curvature_cases"][0]["normalized_local_curvature"] == pytest.approx(0.3)
    assert [case["trade_count_retention_pct"] for case in result["cases"]] == [90.0, 80.0]
    assert result["unstable_peak"] is False


def test_signal_omission_evidence_is_gated_by_layer_count_and_return_retention() -> None:
    result = analyze_signal_omission(
        contract={
            "omission_rates_pct": [25.0],
            "seed_policy": "derived_from_manifest_candidate_scenario_split_contract_hash",
            "min_return_retention_pct": 70.0,
            "min_omitted_entry_signals": 1,
        },
        original_metrics={"return_pct": 10.0},
        runs=(
            {
                "omission_rate_pct": 25.0,
                "return_pct": 8.0,
                "decision_stream_perturbation_evidence": {
                    "layer": "decision_stream_pre_execution",
                    "omitted_entry_signal_count": 2,
                },
            },
        ),
    )

    assert result["status"] == "PASS"
    assert result["cases"][0]["return_retention_pct"] == 80.0


def test_analyzer_defensively_fails_a_required_contract_missing_components() -> None:
    result = analyze_stress_suite(
        contract=StressSuiteContract(required_for_validation=True),
        context=StressSuiteContext(
            manifest_hash="sha256:" + "a" * 64,
            experiment_id="experiment",
            candidate_id="candidate",
            scenario_id="scenario",
            split_name="validation",
            parameter_values={},
        ),
        original_metrics={},
        metrics_v2=None,
        closed_trades=(),
        starting_cash=1_000_000.0,
    )

    assert result["gate_result"] == "FAIL"
    assert result["fail_reasons"] == [
        "stress_suite_required_component_missing:parameter_perturbation",
        "stress_suite_required_component_missing:period_ablation",
        "stress_suite_required_component_missing:signal_omission",
        "stress_suite_required_component_missing:trade_order_monte_carlo",
        "stress_suite_required_component_missing:trade_removal",
    ]


def test_validation_candidate_rejects_execution_model_without_standard_stress_dimensions() -> None:
    model = ExecutionModelConfig(
        scenarios=(
            ExecutionScenario(
                type="fixed_bps",
                fee_rate=0.0005,
                slippage_bps=5.0,
                scenario_role="base",
            ),
        ),
        source="execution_model",
        scenario_policy="single_scenario",
    )

    reasons = validation_execution_stress_policy_reasons(
        research_classification="validated_candidate",
        execution_model=model,
    )

    assert reasons == [
        "validation_execution_stress_policy_required",
        "validation_execution_stress_scenarios_required",
    ]


def test_validation_candidate_accepts_standard_execution_stress_dimensions() -> None:
    model = ExecutionModelConfig(
        scenarios=(
            ExecutionScenario(
                type="fixed_bps",
                fee_rate=0.0005,
                slippage_bps=5.0,
                latency_ms=0,
                partial_fill_rate=0.0,
                order_failure_rate=0.0,
                market_order_extra_cost_bps=0.0,
                scenario_policy="must_pass_base_and_survive_stress",
                scenario_role="base",
            ),
            ExecutionScenario(
                type="stress",
                fee_rate=0.00075,
                slippage_bps=7.5,
                latency_ms=100,
                partial_fill_rate=0.1,
                order_failure_rate=0.05,
                market_order_extra_cost_bps=1.0,
                scenario_policy="must_pass_base_and_survive_stress",
                scenario_role="stress",
            ),
            ExecutionScenario(
                type="stress",
                fee_rate=0.001,
                slippage_bps=10.0,
                latency_ms=200,
                partial_fill_rate=0.2,
                order_failure_rate=0.1,
                market_order_extra_cost_bps=2.0,
                scenario_policy="must_pass_base_and_survive_stress",
                scenario_role="stress",
            ),
        ),
        source="execution_model",
        scenario_policy="must_pass_base_and_survive_stress",
    )

    assert validation_execution_stress_policy_reasons(
        research_classification="validated_candidate",
        execution_model=model,
    ) == []


def test_trade_bootstrap_is_reproducible_and_reports_positive_expectancy_interval() -> None:
    trades = tuple(
        ClosedTradeRecord(exit_ts=index, net_pnl=value)
        for index, value in enumerate([100.0, 120.0, 80.0, 110.0, 90.0] * 2)
    )
    kwargs = {
        "contract": {"iterations": 200, "min_closed_trades": 10},
        "seed_material": {"manifest_hash": "sha256:" + "b" * 64},
        "closed_trades": trades,
        "starting_cash": 10_000.0,
    }

    first = analyze_trade_bootstrap_uncertainty(**kwargs)
    second = analyze_trade_bootstrap_uncertainty(**kwargs)

    assert first == second
    assert first["status"] == "PASS"
    assert first["expectancy_per_trade_krw"]["p025"] > 0.0
    assert first["terminal_return_pct"]["p025"] > 0.0
    assert first["max_drawdown_pct"]["p975"] == 0.0


def test_trade_bootstrap_fails_when_expectancy_interval_includes_zero() -> None:
    trades = tuple(
        ClosedTradeRecord(exit_ts=index, net_pnl=value)
        for index, value in enumerate([100.0, -100.0] * 5)
    )

    result = analyze_trade_bootstrap_uncertainty(
        contract={"iterations": 200, "min_closed_trades": 10},
        seed_material={"manifest_hash": "sha256:" + "c" * 64},
        closed_trades=trades,
        starting_cash=10_000.0,
    )

    assert result["status"] == "FAIL"
    assert "stress_trade_bootstrap_expectancy_not_positive" in result["fail_reasons"]
