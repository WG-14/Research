from __future__ import annotations

from ..config import settings
from ..execution_models import OrderIntent, SubmitPlan
from .base import Broker
from .order_rules import build_buy_price_none_submit_contract
from .order_submit import plan_place_order


def build_live_submit_plan(
    *,
    broker: Broker,
    client_order_id: str,
    side: str,
    qty: float,
    ts: int,
    effective_rules,
    reference_price: float | None,
    market: str | None = None,
    quote_notional_krw: float | None = None,
    quote_notional_authority: str | None = None,
    submit_semantics: str | None = None,
    submit_semantics_authority: str | None = None,
) -> SubmitPlan:
    intent_market = str(market or settings.PAIR or "").strip().upper()
    if not intent_market:
        raise ValueError("live_submit_plan_market_required")
    explicit_submit_contract = (
        build_buy_price_none_submit_contract(rules=effective_rules)
        if side == "BUY"
        else None
    )
    return plan_place_order(
        broker,
        intent=OrderIntent(
            client_order_id=client_order_id,
            market=intent_market,
            side=side,
            normalized_side=("bid" if side == "BUY" else "ask"),
            qty=float(qty),
            price=None,
            created_ts=int(ts),
            submit_contract=explicit_submit_contract,
            quote_notional_krw=quote_notional_krw,
            quote_notional_authority=quote_notional_authority,
            submit_semantics=submit_semantics,
            submit_semantics_authority=submit_semantics_authority,
            market_price_hint=reference_price,
            trace_id=client_order_id,
        ),
        rules=effective_rules,
        skip_qty_revalidation=(side == "SELL"),
    )
