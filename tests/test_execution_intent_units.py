from __future__ import annotations

import pytest

from bithumb_bot.broker import order_rules
from bithumb_bot.broker.base import BrokerRejectError
from bithumb_bot.broker.order_payloads import (
    build_order_payload_from_plan,
    validate_order_submit_payload,
)
from bithumb_bot.execution_models import OrderIntent
from bithumb_bot.execution_planner import build_submit_plan


pytestmark = pytest.mark.fast_regression


def _rules() -> order_rules.DerivedOrderConstraints:
    return order_rules.DerivedOrderConstraints(
        order_types=("limit", "price", "market"),
        bid_types=("price",),
        ask_types=("limit", "market"),
        order_sides=("bid", "ask"),
        bid_min_total_krw=5000.0,
        ask_min_total_krw=5000.0,
        min_notional_krw=5000.0,
        min_qty=0.0001,
        qty_step=0.0001,
        max_qty_decimals=8,
        bid_price_unit=1.0,
        ask_price_unit=1.0,
    )


def _buy_contract(rules):
    return order_rules.build_buy_price_none_submit_contract(
        rules=rules,
        resolution=order_rules.resolve_buy_price_none_resolution(rules=rules),
    )


def _plan(intent: OrderIntent):
    rules = _rules()
    return build_submit_plan(
        intent=intent,
        rules=rules,
        fetch_order_rules=lambda _market: type("Resolution", (), {"rules": rules})(),
        fetch_top_of_book=lambda _market: None,
        resolve_best_ask=lambda _quote, _market: 100_000_120.0,
        truncate_volume=lambda qty: qty,
    )


def test_order_intent_quote_notional_buy_builds_price_payload() -> None:
    rules = _rules()
    plan = _plan(
        OrderIntent(
            client_order_id="quote-intent-buy",
            market="KRW-BTC",
            side="BUY",
            normalized_side="bid",
            qty=100_000.0 / 100_000_120.0,
            price=None,
            created_ts=1,
            submit_contract=_buy_contract(rules),
            quote_notional_krw=100_000.0,
            quote_notional_authority="h74_fixed_fill_quote_notional_buy",
            submit_semantics="quote_notional_market_buy",
            submit_semantics_authority="h74_fixed_fill_quote_notional_buy",
            market_price_hint=100_000_120.0,
        )
    )

    payload = build_order_payload_from_plan(plan=plan).payload

    assert payload == {
        "market": "KRW-BTC",
        "side": "bid",
        "order_type": "price",
        "price": "100000",
        "client_order_id": "quote-intent-buy",
    }
    assert "volume" not in payload


def test_order_intent_base_qty_buy_uses_existing_quantity_floor_path() -> None:
    rules = _rules()
    plan = _plan(
        OrderIntent(
            client_order_id="base-qty-buy",
            market="KRW-BTC",
            side="BUY",
            normalized_side="bid",
            qty=100_000.0 / 100_000_120.0,
            price=None,
            created_ts=1,
            submit_contract=_buy_contract(rules),
            market_price_hint=100_000_120.0,
        )
    )

    assert plan.exchange_submit_notional_krw == pytest.approx(90_000.0)
    assert plan.exchange_constrained_qty == pytest.approx(0.0009)
    assert plan.quote_notional_krw is None


def test_quote_notional_buy_forbids_volume_payload() -> None:
    with pytest.raises(BrokerRejectError, match="order_type=price must not include volume"):
        validate_order_submit_payload(
            {
                "market": "KRW-BTC",
                "side": "bid",
                "order_type": "price",
                "price": "100000",
                "volume": "0.0009",
            }
        )
