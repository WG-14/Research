from __future__ import annotations

from bithumb_bot.research.validation_protocol import _cost_sensitivity_summary


def _scenario(role: str, *, return_pct: float, profit_factor: float = 1.5) -> dict[str, object]:
    return {
        "scenario_id": role,
        "scenario_role": "stress" if role == "stress_cost" else "base",
        "cost_model": {"fee_rate": 0.001 if role != "zero_cost" else 0.0, "slippage_bps": 5.0 if role != "zero_cost" else 0.0},
        "validation_metrics_v2": {
            "total_return_pct": return_pct,
            "profit_factor": profit_factor,
            "trade_count": 4,
            "fee_total": 100.0,
            "slippage_total": 50.0,
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
