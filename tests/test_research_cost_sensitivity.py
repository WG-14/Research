from __future__ import annotations

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.validation_protocol import _apply_scenario_policy, _cost_sensitivity_summary
from tests.test_research_backtest_reproducibility import _manifest


def _scenario(role: str, *, return_pct: float, profit_factor: float = 1.5) -> dict[str, object]:
    return {
        "scenario_id": role,
        "scenario_role": (
            "diagnostic_zero_cost"
            if role == "zero_cost"
            else ("stress" if role == "stress_cost" else "base")
        ),
        "cost_model": {"fee_rate": 0.001 if role != "zero_cost" else 0.0, "slippage_bps": 5.0 if role != "zero_cost" else 0.0},
        "validation_metrics_v2": {
            "return_risk": {"total_return_pct": return_pct},
            "trade_quality": {"profit_factor": profit_factor, "closed_trade_count": 4},
            "cost_execution": {"fee_total": 100.0, "slippage_total": 50.0},
        },
        "cost_assumption": {"role": "diagnostic_zero_cost" if role == "zero_cost" else role},
    }


def test_cost_sensitivity_runs_zero_base_stress_for_same_candidate() -> None:
    summary = _cost_sensitivity_summary(
        [_scenario("zero_cost", return_pct=5.0), _scenario("base_cost", return_pct=3.0), _scenario("stress_cost", return_pct=1.0)]
    )

    assert set(summary) >= {"zero_cost", "base_cost", "stress_cost"}
    assert summary["zero_cost"]["validation_return_pct"] == 5.0
    assert summary["base_cost"]["validation_profit_factor"] == 1.5
    assert summary["stress_cost"]["validation_trade_count"] == 4


def test_zero_cost_diagnostic_does_not_make_candidate_promotable() -> None:
    summary = _cost_sensitivity_summary([_scenario("zero_cost", return_pct=20.0), _scenario("base_cost", return_pct=-1.0)])

    assert summary["zero_cost"]["promotable_as_base"] is False
    assert summary["promotion_authority"] == "diagnostic_only_zero_cost_excluded_from_promotion"


def test_cost_drag_fields_are_persisted_in_candidate_result_summary() -> None:
    summary = _cost_sensitivity_summary(
        [_scenario("zero_cost", return_pct=5.0), _scenario("base_cost", return_pct=3.0), _scenario("stress_cost", return_pct=1.0)]
    )

    assert summary["fee_drag_ratio"] is not None
    assert summary["slippage_drag_ratio"] is not None
    assert summary["cost_breakeven_trade_edge"] == 37.5


def test_cost_sensitivity_requires_real_zero_cost_scenario_or_marks_missing() -> None:
    summary = _cost_sensitivity_summary([_scenario("base_cost", return_pct=3.0)])

    assert summary["zero_cost"]["status"] == "missing"
    assert summary["zero_cost"]["synthetic"] is True
    assert summary["zero_cost"]["validation_return_pct"] is None
    assert summary["zero_cost"]["missing_reason"] == "real_zero_cost_scenario_result_absent"


def test_cost_sensitivity_zero_cost_is_not_copied_from_base_metrics() -> None:
    summary = _cost_sensitivity_summary([_scenario("base_cost", return_pct=3.0)])

    assert summary["base_cost"]["validation_return_pct"] == 3.0
    assert summary["zero_cost"]["validation_return_pct"] != summary["base_cost"]["validation_return_pct"]


def test_cost_sensitivity_reads_nested_metrics_v2_return_and_costs() -> None:
    scenario = _scenario("base_cost", return_pct=1.23)
    scenario["validation_metrics"] = {"return_pct": 9.99, "fee_total": 999, "slippage_total": 999}
    scenario["validation_metrics_v2"]["cost_execution"]["fee_total"] = 400
    scenario["validation_metrics_v2"]["cost_execution"]["slippage_total"] = 0

    summary = _cost_sensitivity_summary([scenario])

    assert summary["base_cost"]["validation_return_pct"] == 1.23
    assert summary["base_cost"]["fee_total"] == 400
    assert summary["base_cost"]["slippage_total"] == 0


def test_cost_sensitivity_falls_back_to_legacy_flat_metrics() -> None:
    scenario = {
        "scenario_id": "base",
        "scenario_role": "base",
        "cost_model": {"fee_rate": 0.0004, "slippage_bps": 10.0},
        "validation_metrics": {
            "return_pct": 2.5,
            "profit_factor": 1.4,
            "trade_count": 7,
            "fee_total": 12.0,
            "slippage_total": 3.0,
        },
    }

    summary = _cost_sensitivity_summary([scenario])

    assert summary["base_cost"]["validation_return_pct"] == 2.5
    assert summary["base_cost"]["validation_profit_factor"] == 1.4
    assert summary["base_cost"]["validation_trade_count"] == 7
    assert summary["base_cost"]["fee_total"] == 12.0
    assert summary["base_cost"]["slippage_total"] == 3.0


def test_research_run_materializes_real_zero_cost_scenario_for_cost_sensitivity() -> None:
    payload = _manifest()
    payload["execution_model"] = {
        "type": "stress",
        "fee_rate": [0.0004],
        "slippage_bps": [5.0, 20.0],
        "scenario_policy": "must_pass_base_and_survive_stress",
    }

    manifest = parse_manifest(payload)

    assert manifest.execution_model.scenario_policy == "must_pass_base_and_survive_stress"
    assert [scenario.scenario_role for scenario in manifest.execution_model.scenarios] == [
        "diagnostic_zero_cost",
        "base",
        "stress",
    ]
    zero = manifest.execution_model.scenarios[0]
    assert zero.fee_rate == 0.0
    assert zero.slippage_bps == 0.0
    assert zero.cost_assumption is not None
    assert zero.cost_assumption.role == "diagnostic_zero_cost"
    assert zero.cost_assumption.promotable_as_base is False


def test_zero_cost_diagnostic_role_is_excluded_from_promotion_policy() -> None:
    manifest = parse_manifest(_manifest() | {"execution_model": {
        "type": "stress",
        "fee_rate": [0.0004],
        "slippage_bps": [5.0, 20.0],
        "scenario_policy": "must_pass_base_and_survive_stress",
    }})
    candidate = {
        "scenario_policy": "must_pass_base_and_survive_stress",
        "scenario_results": [
            _scenario("zero_cost", return_pct=20.0),
            {
                **_scenario("base_cost", return_pct=2.0),
                "scenario_acceptance_gate_result": "PASS",
            },
            {
                **_scenario("stress_cost", return_pct=-1.0),
                "scenario_acceptance_gate_result": "FAIL",
                "scenario_fail_reasons": ["stress_failed"],
            },
        ],
    }
    candidate["scenario_results"][0]["scenario_acceptance_gate_result"] = "PASS"

    _apply_scenario_policy(manifest=manifest, candidate=candidate)

    assert candidate["diagnostic_scenario_count"] == 1
    assert candidate["required_scenario_count"] == 2
    assert candidate["acceptance_gate_result"] == "FAIL"
    assert "zero_cost" not in candidate["required_scenario_ids"]
    assert "scenario_policy_required_scenario_failed:stress_cost:stress_failed" in candidate["gate_fail_reasons"]
