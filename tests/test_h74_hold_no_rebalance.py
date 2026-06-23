from __future__ import annotations

from bithumb_bot.entry_authority import evaluate_entry_authority
from bithumb_bot.experiment_execution_contract import POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT
from bithumb_bot.target_position import TargetPositionSettings, build_target_position_decision


def _settings(position_mode: str = POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT) -> TargetPositionSettings:
    return TargetPositionSettings(
        execution_engine="target_delta",
        target_exposure_krw=100_000.0,
        max_order_krw=100_000.0,
        position_mode=position_mode,
    )


def _readiness() -> dict[str, object]:
    return {
        "broker_position_evidence": {"broker_qty_known": True, "broker_qty": 0.0008},
        "projection_converged": True,
        "projection_convergence": {"converged": True},
        "previous_target_exposure_krw": 100_000.0,
    }


def test_h74_holding_price_down_does_not_buy_existing_target_rebalance() -> None:
    decision = build_target_position_decision(
        raw_signal="HOLD",
        previous_target_exposure_krw=100_000.0,
        current_position_snapshot=None,
        readiness_payload=_readiness(),
        order_rules={"min_qty": 0.0001, "qty_step": 0.0001, "min_notional_krw": 5000.0},
        reference_price=100_000_000.0,
        settings=_settings(),
    )
    authority = evaluate_entry_authority(
        payload={"final_signal": "HOLD", "position_mode": POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT, "previous_target_exposure_krw": 100_000},
        side="BUY",
        current_exposure_krw=80_000,
        target_exposure_krw=100_000,
        delta_krw=20_000,
    )

    assert decision.would_submit is False
    assert authority.reason_code != "existing_target_rebalance"


def test_h74_holding_price_up_does_not_sell_until_exit_due() -> None:
    decision = build_target_position_decision(
        raw_signal="HOLD",
        previous_target_exposure_krw=100_000.0,
        current_position_snapshot=None,
        readiness_payload=_readiness(),
        order_rules={"min_qty": 0.0001, "qty_step": 0.0001, "min_notional_krw": 5000.0},
        reference_price=166_666_666.0,
        settings=_settings(),
    )

    assert decision.would_submit is False
    assert decision.delta_side == "NONE"


def test_h74_holding_no_submit_proof_contains_cycle_id_and_reason() -> None:
    decision = build_target_position_decision(
        raw_signal="HOLD",
        previous_target_exposure_krw=100_000.0,
        current_position_snapshot=None,
        readiness_payload={**_readiness(), "h74_cycle_id": "cycle-1"},
        order_rules={"min_qty": 0.0001, "qty_step": 0.0001, "min_notional_krw": 5000.0},
        reference_price=100_000_000.0,
        settings=_settings(),
    )

    assert decision.as_dict()["h74_cycle_id"] == "cycle-1"
    assert decision.as_dict()["h74_no_submit_reason"] == "h74_fixed_position_hold"


def test_general_target_delta_existing_target_rebalance_still_allowed() -> None:
    authority = evaluate_entry_authority(
        payload={"final_signal": "HOLD", "previous_target_exposure_krw": 100_000},
        side="BUY",
        current_exposure_krw=80_000,
        target_exposure_krw=100_000,
        delta_krw=20_000,
    )

    assert authority.reason_code == "existing_target_rebalance"
