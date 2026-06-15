from __future__ import annotations

from types import SimpleNamespace

from bithumb_bot.research.validation_protocol import _metrics_v2_gate_reasons


def _gate(**overrides):
    values = {
        "min_cagr_pct": None,
        "min_expectancy_per_trade_krw": None,
        "min_expectancy_per_trade_pct": None,
        "max_exposure_time_pct": None,
        "max_avg_holding_time_minutes": None,
        "max_fee_drag_ratio": None,
        "max_slippage_drag_ratio": None,
        "max_single_trade_dependency_score": None,
        "reject_open_position_at_end": False,
        "metrics_contract_required": False,
        "min_trade_days_pct": None,
        "max_zero_filled_days": None,
        "max_consecutive_zero_filled_days": None,
        "min_filled_execution_per_kst_day": None,
        "participation_count_basis": "filled",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _metrics(**overrides):
    participation = {
        "count_basis": "filled",
        "calendar_day_count": 30,
        "days_with_filled_execution": 29,
        "zero_filled_days": 1,
        "max_consecutive_zero_filled_days": 1,
        "min_daily_filled_execution_count": 0,
    }
    participation.update(overrides)
    return {
        "metrics_schema_version": 2,
        "return_risk": {},
        "trade_quality": {},
        "time_exposure": {},
        "cost_execution": {},
        "participation": participation,
    }


def test_gate_fails_when_zero_filled_days_exceeds_limit() -> None:
    reasons = _metrics_v2_gate_reasons(gate=_gate(max_zero_filled_days=0), metrics_v2=_metrics(), prefix="")

    assert "daily_participation_max_zero_filled_days_failed" in reasons


def test_gate_fails_when_trades_clustered_in_one_day() -> None:
    reasons = _metrics_v2_gate_reasons(
        gate=_gate(min_trade_days_pct=100.0),
        metrics_v2=_metrics(days_with_filled_execution=1, zero_filled_days=29),
        prefix="",
    )

    assert "daily_participation_min_trade_days_pct_failed" in reasons


def test_gate_result_includes_daily_participation_fail_reason() -> None:
    reasons = _metrics_v2_gate_reasons(
        gate=_gate(max_consecutive_zero_filled_days=0),
        metrics_v2=_metrics(),
        prefix="",
    )

    assert any(reason.startswith("daily_participation_") for reason in reasons)

