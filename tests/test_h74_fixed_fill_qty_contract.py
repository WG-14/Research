from __future__ import annotations

import pytest

from bithumb_bot.experiment_execution_contract import POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT
from bithumb_bot.target_position import TargetPositionSettings, build_target_position_decision


def _readiness(price_qty: float = 0.0008) -> dict[str, object]:
    return {
        "broker_position_evidence": {"broker_qty_known": True, "broker_qty": price_qty},
        "projection_converged": True,
        "projection_convergence": {"converged": True},
        "open_order_count": 0,
        "submit_unknown_count": 0,
        "recovery_required_count": 0,
        "accounting_projection_ok": True,
    }


def _rules() -> dict[str, object]:
    return {"min_qty": 0.0001, "qty_step": 0.0001, "min_notional_krw": 5000.0}


def _settings(mode: str = POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT) -> TargetPositionSettings:
    return TargetPositionSettings(
        execution_engine="target_delta",
        target_exposure_krw=100_000.0,
        max_order_krw=100_000.0,
        position_mode=mode,
    )


def test_h74_hold_does_not_rebalance_after_entry_fill_when_price_moves_down() -> None:
    decision = build_target_position_decision(
        raw_signal="HOLD",
        previous_target_exposure_krw=100_000.0,
        current_position_snapshot=None,
        readiness_payload=_readiness(),
        order_rules=_rules(),
        reference_price=100_000_000.0,
        settings=_settings(),
    )

    assert decision.would_submit is False
    assert decision.delta_side == "NONE"
    assert decision.block_reason == "h74_fixed_position_hold"


def test_h74_hold_does_not_rebalance_after_entry_fill_when_price_moves_up() -> None:
    decision = build_target_position_decision(
        raw_signal="HOLD",
        previous_target_exposure_krw=100_000.0,
        current_position_snapshot=None,
        readiness_payload=_readiness(),
        order_rules=_rules(),
        reference_price=166_666_666.0,
        settings=_settings(),
    )

    assert decision.would_submit is False
    assert decision.delta_side == "NONE"


def test_h74_exit_sells_remaining_entry_fill_qty_not_target_exposure_qty() -> None:
    decision = build_target_position_decision(
        raw_signal="SELL",
        previous_target_exposure_krw=100_000.0,
        current_position_snapshot=None,
        readiness_payload={
            **_readiness(price_qty=0.0028),
            "h74_cycle_id": "cycle-1",
            "remaining_cycle_qty": 0.0008,
        },
        order_rules=_rules(),
        reference_price=125_000_000.0,
        settings=_settings(),
    )

    assert decision.would_submit is True
    assert decision.delta_side == "SELL"
    assert decision.submit_qty == pytest.approx(0.0008)
    assert decision.submit_notional_krw == pytest.approx(100_000.0)


def test_non_h74_target_delta_still_rebalances_existing_target() -> None:
    decision = build_target_position_decision(
        raw_signal="HOLD",
        previous_target_exposure_krw=100_000.0,
        current_position_snapshot=None,
        readiness_payload=_readiness(),
        order_rules=_rules(),
        reference_price=100_000_000.0,
        settings=_settings(mode="continuous_notional_target"),
    )

    assert decision.would_submit is True
    assert decision.delta_side == "BUY"
