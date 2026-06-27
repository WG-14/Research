from __future__ import annotations

import pytest

from bithumb_bot.h74_cycle_state import build_h74_cycle_closeout_plan_from_payload
from bithumb_bot.order_sizing import build_target_delta_execution_sizing


def _payload(**overrides):
    payload = {
        "cycle_id": "h74-cycle-1",
        "h74_cycle_id": "h74-cycle-1",
        "entry_client_order_id": "h74-entry-order",
        "h74_entry_plan_client_order_id": "h74-entry-plan",
        "contract_hash": "sha256:" + "a" * 64,
        "h74_position_ownership_contract_hash": "sha256:" + "a" * 64,
        "authority_hash": "sha256:" + "b" * 64,
        "strategy_instance_id": "h74-source-observation",
        "acquired_qty": 0.00109271,
        "sold_qty": 0.0,
        "locked_exit_qty": 0.0,
        "remaining_cycle_qty": 0.00109271,
        "broker_available_qty": 0.00109271,
    }
    payload.update(overrides)
    return payload


def test_h74_closeout_plan_uses_cycle_remaining_qty() -> None:
    plan = build_h74_cycle_closeout_plan_from_payload(
        _payload(),
        target_delta_side="SELL",
        target_qty=0.0,
    )
    sizing = build_target_delta_execution_sizing(
        pair="KRW-BTC",
        side="SELL",
        desired_qty=plan.closeout_qty,
        market_price=100_000_000.0,
        min_qty=0.001,
        qty_step=0.0,
        min_notional_krw=5_000.0,
        max_qty_decimals=8,
        authority_source=plan.qty_authority,
        h74_closeout=True,
        qty_step_authority="local_fallback_min_qty",
    )

    assert plan.closeout_qty == pytest.approx(0.00109271)
    assert sizing.final_submitted_qty == pytest.approx(0.00109271)
    assert sizing.final_submitted_qty != pytest.approx(0.001)


def test_h74_closeout_plan_requires_entry_plan_id_and_contract_hash() -> None:
    with pytest.raises(ValueError, match="h74_entry_plan_client_order_id"):
        build_h74_cycle_closeout_plan_from_payload(
            _payload(h74_entry_plan_client_order_id="", entry_client_order_id=""),
            target_delta_side="SELL",
            target_qty=0.0,
        )
    with pytest.raises(ValueError, match="h74_position_ownership_contract_hash"):
        build_h74_cycle_closeout_plan_from_payload(
            _payload(contract_hash="", h74_position_ownership_contract_hash=""),
            target_delta_side="SELL",
            target_qty=0.0,
        )


def test_h74_closeout_plan_rejects_missing_cycle_identity() -> None:
    with pytest.raises(ValueError, match="cycle_id"):
        build_h74_cycle_closeout_plan_from_payload(
            _payload(cycle_id="", h74_cycle_id=""),
            target_delta_side="SELL",
            target_qty=0.0,
        )


def test_h74_closeout_plan_rejects_qty_below_remaining_without_residual_policy() -> None:
    plan = build_h74_cycle_closeout_plan_from_payload(
        _payload(),
        target_delta_side="SELL",
        target_qty=0.0,
    )
    submitted_qty = 0.001
    residual_policy = "none"

    assert submitted_qty < plan.remaining_qty
    assert residual_policy == "none"
    with pytest.raises(ValueError, match="h74_closeout_qty_below_remaining_without_residual_policy"):
        if submitted_qty + 1e-12 < plan.remaining_qty and residual_policy == "none":
            raise ValueError("h74_closeout_qty_below_remaining_without_residual_policy")
