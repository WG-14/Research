from __future__ import annotations

import pytest

from bithumb_bot.order_sizing import build_target_delta_execution_sizing
from bithumb_bot.quantity_kernel import OrderRuleSnapshot, plan_h74_closeout_qty


def test_h74_closeout_does_not_use_min_qty_as_step_when_qty_step_missing() -> None:
    result = plan_h74_closeout_qty(
        remaining_qty=0.00109271,
        reference_price=100_000_000.0,
        rules=OrderRuleSnapshot(
            min_qty=0.001,
            qty_step=0.0,
            max_qty_decimals=8,
            min_notional_krw=5_000.0,
            qty_step_authority="local_fallback_min_qty",
        ),
    )

    assert result.allowed is True
    assert result.submitted_qty == pytest.approx(0.00109271)
    assert result.submitted_qty != pytest.approx(0.001)
    assert result.qty_step_authority == "local_fallback_min_qty"


def test_generic_target_delta_sell_still_floors_by_exchange_step() -> None:
    sizing = build_target_delta_execution_sizing(
        pair="KRW-BTC",
        side="SELL",
        desired_qty=0.00109271,
        market_price=100_000_000.0,
        min_qty=0.001,
        qty_step=0.001,
        min_notional_krw=5_000.0,
        max_qty_decimals=8,
        authority_source="target_delta.desired_delta",
    )

    assert sizing.allowed is True
    assert sizing.final_submitted_qty == pytest.approx(0.001)
    assert sizing.qty_step_authority == "exchange"


def test_h74_closeout_records_residual_when_exchange_step_forces_floor() -> None:
    result = plan_h74_closeout_qty(
        remaining_qty=0.00109271,
        reference_price=100_000_000.0,
        rules=OrderRuleSnapshot(
            min_qty=0.001,
            qty_step=0.001,
            max_qty_decimals=8,
            min_notional_krw=5_000.0,
            qty_step_authority="exchange",
        ),
    )

    assert result.allowed is True
    assert result.submitted_qty == pytest.approx(0.001)
    assert result.residual_qty == pytest.approx(0.00009271)
    assert result.residual_policy == "exchange_step_residual_tracked"
    assert result.residual_reason == "exchange_step_constraint"
